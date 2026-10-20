"""pywebview windows and the JS bridge.

Two windows: a small always-on-top island and a full panel. The UI is static
files under ui/ — no build step, no bundler, no npm.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import webview

from . import claude_cli as cli
from . import classifier
from . import flags as flagmod
from .bus import EventBus
from .connectors.base import SendRefused
from .connectors.claude_source import CalendarSource
from .connectors.demo import DEMO_SETTINGS, DemoSource
from .connectors.mail_source import MailSource, describe_voice
from .connectors.pending import PendingSource
from .connectors.slack_source import SlackSource
from .hotkeys import BINDINGS, Hotkeys
from .models import ApprovalToken, Flag, Item, SourceStatus, now_iso
from .privacy import MEETING_APPS, PrivacyWatcher
from . import summaries
from .scheduler import Scheduler, in_quiet_hours
from .store import Store
from .tray import Tray

ME = "jose.gonzalez@sailpoint.com"

UI = Path(__file__).resolve().parent.parent / "ui"

ISLAND_W, ISLAND_H = 360, 44
PANEL_W, PANEL_H = 980, 640

DEFAULT_SHORTCUTS = [
    ("Jira", "https://sailpoint.atlassian.net", "Work"),
    ("Salesforce", "https://sailpoint.lightning.force.com", "Work"),
    ("Confluence", "https://sailpoint.atlassian.net/wiki", "Reference"),
    ("Workday", "https://www.myworkday.com", "Admin"),
]


class Api:
    """Everything the UI can call. Nothing here sends without an explicit click."""

    def __init__(self, store: Store, bus: EventBus, demo: bool,
                 availability: cli.Availability | None = None) -> None:
        self._store = store
        self._bus = bus
        self._demo = demo
        self._availability = availability or cli.Availability()
        self._sources = self._build_sources()
        self._scheduler: Scheduler | None = None
        self._island = None
        self._panel = None
        self._chat_lock = threading.Lock()
        self._privacy = PrivacyWatcher(
            bus,
            enabled=lambda: bool(self._store.get_setting("privacy_auto", True)),
            apps=lambda: set(self._store.get_setting("privacy_apps") or MEETING_APPS),
        )

    def _build_sources(self) -> dict:
        if self._demo:
            return {n: DemoSource(n) for n in ("mail", "slack", "calendar")}
        # Live mode never pads a gap with fixtures: a panel that mixes real and fake
        # data is worse than one that says a source is not built yet.
        domains = self._store.get_setting("internal_domains") or []
        return {
            "mail": MailSource(ME, domains, self._availability, store=self._store),
            "slack": SlackSource(ME, domains, self._availability,
                                 store=self._store),
            "calendar": CalendarSource(ME, domains, self._availability),
        }

    # ── windows ─────────────────────────────────────────────────────────────

    def open_panel(self) -> None:
        if self._panel is not None:
            self._panel.show()
            try:
                self._panel.on_top = True
                self._panel.on_top = False
            except Exception:
                pass

    def hide_panel(self) -> None:
        if self._panel is not None:
            self._panel.hide()

    # ── privacy ─────────────────────────────────────────────────────────────

    def privacy_state(self) -> dict:
        return self._privacy.state().to_dict()

    def set_privacy(self, on: bool | None = None) -> dict:
        """The panic hotkey and the manual toggle. Python owns the truth so both
        windows agree — a per-window CSS flag would drift."""
        return self._privacy.set_manual(on).to_dict()

    def may_interrupt(self) -> dict:
        """Whether the island is allowed to pop. Checked before every interruption.

        A notification during a shared screen or over a fullscreen app is the one
        kind HELM can actually do harm with, so each refusal names its reason.
        """
        st = self._privacy.state()
        if st.on:
            return {"ok": False, "reason": st.reason or "privacy mode is on"}
        if self._privacy.foreground_is_fullscreen:
            return {"ok": False, "reason": "a fullscreen app is in front"}
        quiet = self._store.get_setting("quiet_hours") or {}
        if in_quiet_hours(datetime.now(), quiet.get("start", "19:00"),
                          quiet.get("end", "07:00")):
            return {"ok": False, "reason": "quiet hours"}
        return {"ok": True, "reason": ""}

    def resize_island(self, height: int) -> None:
        """The island grows and shrinks with its content; the UI knows the height."""
        if self._island is None:
            return
        try:
            self._island.resize(ISLAND_W, max(8, int(height)))
        except Exception as e:
            # Swallowing this hid the fact that the island window keeps its launch
            # height, so its transparent area sits over the screen it is not using.
            print(f"[helm] island resize to {height} failed: {e}")

    # ── data ────────────────────────────────────────────────────────────────

    def _settings_obj(self) -> flagmod.Settings:
        s = self._store.all_settings()
        return flagmod.Settings(
            vips=set(s.get("vips") or []),
            managers=set(s.get("managers") or []),
            muted_senders=set(s.get("muted_senders") or []),
            muted_channels=set(s.get("muted_channels") or []),
            internal_domains=set(s.get("internal_domains") or []),
            keyword_weights=dict(s.get("keyword_weights") or {}),
            sender_profiles=self._store.sender_profiles(),
        )

    def poll(self, source: str) -> bool:
        """One sweep of one source. Returns False so the scheduler can back off.

        A source that fails keeps the rows it already had and flips its status dot;
        it never empties the list, because an inbox that looks calm because the
        connector broke is the one failure that makes the whole panel untrustworthy.
        """
        src = self._sources.get(source)
        if src is None:
            return False

        cfg = self._settings_obj()
        t0 = time.perf_counter()
        try:
            items = src.list_new("")
        except Exception as e:
            self._store.set_source_status(SourceStatus(
                source=source, health="reconnecting", last_error=str(e)[:300]))
            self._bus.publish("source_status", {"source": source, "error": str(e)[:300]})
            return False

        seen: set[str] = set()
        for item in items:
            seen.add(item.id)
            known = self._store.item(item.id)
            self._store.upsert_item(item)
            existing = self._store.flag(item.id)
            flag = flagmod.classify(item, cfg, existing=existing)
            self._store.set_flag(flag)
            # A thread can cost its own connector call, so only sources that can
            # produce one for free are asked during a sweep. The rest fetch when the
            # item is opened. Pulling 25 mail threads per sweep would turn two
            # minutes into half an hour.
            if getattr(src, "prefetch_threads", True):
                thread = src.get_thread(item.id)
                if thread.messages:
                    self._store.set_thread(item.id, thread.messages)
            if known is None and source != "calendar":
                self._bus.publish("new_email" if source == "mail" else "slack_mention",
                                  {"item_id": item.id, "subject": item.subject})

        # Two-way sync: anything the source stopped returning was handled elsewhere.
        st = src.status()
        if st.health == "live":
            self._retire_missing(source, seen)

        if not st.measured_latency_ms:
            st.measured_latency_ms = int((time.perf_counter() - t0) * 1000)
        st.count = len([i for i in items if i.state == "needs_me"])
        self._store.set_source_status(st)
        self._bus.publish("source_status", {"source": source, "health": st.health,
                                            "count": st.count})
        if st.health == "live" and not self._demo:
            self._classify_later()
        return st.health in ("live", "demo")

    # ── stage 2 ─────────────────────────────────────────────────────────────

    def classify_ambiguous(self, limit: int = classifier.BATCH) -> dict:
        """Ask Claude about the items the rules could not settle.

        Runs after a sweep, in the background, so a slow verdict never blocks the
        panel. Importance only: urgency stays with the rules. A verdict about the
        sender is remembered, so the same question is never paid for twice.
        """
        if self._demo:
            return {"skipped": "demo mode classifies with rules only"}

        cfg = self._settings_obj()
        items = self._store.items()
        flags = self._store.flags()
        profiles = self._store.sender_profiles()
        batch = classifier.candidates(items, flags, cfg, profiles)[:limit]
        if not batch:
            return {"asked": 0, "note": "the rules settled everything"}

        try:
            verdicts = classifier.classify_batch(batch)
        except cli.ClaudeUnavailable as e:
            self._store.log_action("classify", f"{len(batch)} items", "flags",
                                   str(e)[:200], "failed")
            return {"error": str(e)[:200], "asked": len(batch)}

        by_id = {i.id: i for i in batch}
        changed = 0
        for verdict in verdicts:
            item = by_id.get(verdict["id"])
            if item is None:
                continue
            flag = self._store.flag(item.id) or Flag(item_id=item.id)
            if flag.pinned:
                continue
            before = flag.importance
            flag = classifier.apply(verdict, flag)
            flag.recheck_after = classifier.next_recheck()
            self._store.set_flag(flag)
            if item.sender_addr:
                self._store.set_sender_profile(item.sender_addr,
                                               **classifier.profile_from(verdict))
            if flag.importance != before:
                changed += 1

        self._store.log_action("classify", f"{len(batch)} items", "flags",
                               f"{len(verdicts)} verdicts, {changed} changed", "done")
        self._bus.publish("flag_changed", {"all": True, "claude": len(verdicts)})
        return {"asked": len(batch), "answered": len(verdicts), "changed": changed}

    def _classify_later(self) -> None:
        threading.Thread(target=self.classify_ambiguous, daemon=True,
                         name="helm-classify").start()

    def _retire_missing(self, source: str, seen: set[str]) -> None:
        for item in self._store.items(source=source):
            if item.id not in seen and item.state in ("needs_me", "working"):
                self._store.set_item_state(item.id, "done", replied_at=now_iso())
                self._bus.publish("item_changed", {"item_id": item.id, "state": "done"})

    def sync(self) -> dict:
        """Sweep every source once. Used at launch and by the manual refresh."""
        counts: dict[str, int] = {}
        for name in self._sources:
            self.poll(name)
            counts[name] = (self._sources[name].status().count or 0)
        return counts

    def resync(self) -> dict:
        """Manual refresh from the UI."""
        if self._scheduler is not None:
            self._scheduler.poke("you asked")
            return {"ok": True, "queued": True}
        self.sync()
        return {"ok": True, "queued": False}

    def schedule_info(self) -> dict:
        """What Settings shows: the real cadence and the measured latency, not a promise."""
        info = self._scheduler.describe() if self._scheduler else {"sources": {}}
        info["availability"] = {
            "cli": bool(self._availability.cli),
            "microsoft_365": self._availability.microsoft_365,
            "slack": self._availability.slack,
            "error": self._availability.error,
        }
        info["demo"] = self._demo
        return info

    def bootstrap(self) -> dict:
        """One call the UI makes on load. Everything it needs to paint."""
        return {
            "settings": self._store.all_settings(),
            "demo": self._demo,
            "sources": self._store.source_states(),
            "items": self.list_items(),
            "shortcuts": self._store.shortcuts(),
            "hotkeys": {k: "+".join([v[0], v[1]]) for k, v in BINDINGS.items()},
            "action_log": self._store.action_log(50),
            "schedule": self.schedule_info(),
            "voice": describe_voice(self._store.get_setting("voice_profile", {})),
            "senders": self.learned_senders(),
            # Carried here rather than fetched separately: two bridge round-trips at
            # boot raced each other and left the island unrendered.
            "privacy": self.privacy_state(),
            "search": {"available": self._store.search_available,
                       "indexed": self._store.search_size()},
            "summaries": self.summaries(),
        }

    def list_items(self) -> list[dict]:
        # Sort on the objects already in hand. Re-reading the store inside the sort
        # key cost four queries per row, and both windows call this at launch, which
        # is what made the island take seconds to paint.
        pairs = [(item, self._store.flag(item.id) or Flag(item_id=item.id))
                 for item in self._store.items()]
        pairs.sort(key=lambda p: flagmod.focus_sort_key(p[1], p[0]))
        return [{"item": item.to_dict(), "flag": flag.to_dict()} for item, flag in pairs]

    # ── summaries and commitments ───────────────────────────────────────────

    def summaries(self) -> dict:
        """Every summary, computed from rows already in the store.

        No connector call and no model call, so this is free and right offline.
        """
        rows = self.list_items()
        log = self._store.action_log(500)
        return {
            "brief": summaries.morning_brief(rows),
            "recap": summaries.end_of_day(rows),
            "weekly": summaries.weekly_metrics(rows, log),
            "commitments": self._store.commitments(),
            "last_run": self._store.get_setting("summary_last_run", {}),
        }

    def refresh_commitments(self) -> dict:
        """Re-extract promises from your own sent messages. Local, deterministic."""
        found = summaries.extract_commitments(self._store.my_messages())
        kept = self._store.replace_commitments([c.to_dict() for c in found])
        self._bus.publish("item_changed", {"commitments": kept})
        return {"found": len(found), "open": kept,
                "commitments": self._store.commitments()}

    def set_commitment_state(self, commitment_id: int, state: str) -> dict:
        self._store.set_commitment_state(int(commitment_id), state)
        self._store.log_action("commitment", str(commitment_id), "local", state, "done")
        return {"commitments": self._store.commitments()}

    def _run_due_summaries(self) -> None:
        """Generate whatever is due, once per day, and tell the UI it is there."""
        times = self._store.get_setting("summary_times") or {}
        last = dict(self._store.get_setting("summary_last_run") or {})
        due = summaries.due_now(datetime.now().astimezone(), times, last)
        if not due.any():
            return
        today = datetime.now().astimezone().date().isoformat()
        if due.brief or due.weekly:
            # A brief is about what you owe, so the promise list is rebuilt first.
            self.refresh_commitments()
        for kind in ("brief", "recap", "weekly"):
            if getattr(due, kind):
                last[kind] = today
        self._store.set_setting("summary_last_run", last)
        for reason in due.reasons:
            self._store.log_action("summary", reason, "local", "", "generated")
        self._bus.publish("summary_ready", {"due": due.reasons})

    # ── search ──────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 40) -> dict:
        """Local index only: instant, offline, no connector call and no model call."""
        if not self._store.search_available:
            return {"available": False, "results": [], "indexed": 0}
        hits = self._store.search(query, limit)
        results = []
        for hit in hits:
            item = self._store.item(hit["item_id"])
            if item is None:
                continue        # an item retired since it was indexed
            flag = self._store.flag(item.id) or Flag(item_id=item.id)
            results.append({"item": item.to_dict(), "flag": flag.to_dict(),
                            "excerpt": hit["excerpt"], "field": hit["field"]})
        return {"available": True, "results": results,
                "indexed": self._store.search_size()}

    def reindex(self) -> dict:
        return {"items": self._store.reindex(), "indexed": self._store.search_size()}

    def item_detail(self, item_id: str) -> dict:
        item = self._store.item(item_id)
        if item is None:
            return {"error": "not found"}
        flag = self._store.flag(item_id) or Flag(item_id=item_id)
        thread = self._store.thread(item_id)
        src = self._sources.get(item.source)

        # A thread we do not have yet is fetched in the background: the drawer opens
        # now with a labelled placeholder rather than freezing for two minutes.
        fetching = False
        if not thread and not getattr(src, "prefetch_threads", True):
            fetching = True
            threading.Thread(target=self._fetch_thread, args=(item_id,),
                             daemon=True, name="helm-thread").start()

        return {
            "item": item.to_dict(),
            "flag": flag.to_dict(),
            "thread": thread,
            "thread_loading": fetching,
            "chat": self._store.chat(item_id),
            "needs_reply_reason": (src.reply_reason(item_id)
                                   if hasattr(src, "reply_reason") else ""),
            "voice": describe_voice(self._store.get_setting("voice_profile", {})),
            "senders": self.learned_senders(),
            # Carried here rather than fetched separately: two bridge round-trips at
            # boot raced each other and left the island unrendered.
            "privacy": self.privacy_state(),
            "search": {"available": self._store.search_available,
                       "indexed": self._store.search_size()},
            "summaries": self.summaries(),
            # What the chat drawer is allowed to attach, stated rather than implied.
            "attached": [
                {"label": "this message", "included": True},
                {"label": (f"{len(thread)} messages in thread" if thread
                           else "the thread, once it loads" if fetching
                           else "no thread found"),
                 "included": bool(thread) or fetching},
                {"label": "your writing style, from your own sent mail",
                 "included": bool(self._store.get_setting("voice_profile", {}))},
                {"label": "nothing else from your mailbox", "included": False},
            ],
        }

    def _fetch_thread(self, item_id: str) -> None:
        item = self._store.item(item_id)
        src = self._sources.get(item.source) if item else None
        if src is None:
            return
        try:
            thread = src.get_thread(item_id)
        except Exception as e:
            self._push(f"helm.onThreadLoaded({json.dumps(item_id)}, [], "
                       f"{json.dumps(str(e)[:200])})")
            return
        if thread.messages:
            self._store.set_thread(item_id, thread.messages)
        self._push(f"helm.onThreadLoaded({json.dumps(item_id)}, "
                   f"{json.dumps(thread.messages)}, "
                   f"{json.dumps('' if thread.messages else 'no thread found')})")

    def set_item_state(self, item_id: str, state: str) -> dict:
        extra = {}
        if state == "done":
            extra["replied_at"] = now_iso()
        if state == "snoozed":
            extra["snooze_until"] = _next_morning().isoformat(timespec="seconds")
        self._store.set_item_state(item_id, state, **extra)
        self._store.log_action("state", item_id, "local", state, "done")
        self._bus.publish("item_changed", {"item_id": item_id, "state": state})
        return {"ok": True}

    # ── flags ───────────────────────────────────────────────────────────────

    def flag_override(self, item_id: str, kind: str) -> dict:
        flag = self._store.flag(item_id)
        if flag is None:
            return {"error": "no flag"}
        flag = flagmod.override(flag, kind)
        self._store.set_flag(flag)
        self._learn(item_id, kind)
        self._bus.publish("flag_changed", {"item_id": item_id, "flag": flag.to_dict()})
        return flag.to_dict()

    def _learn(self, item_id: str, kind: str) -> None:
        """Local learning. One click teaches HELM something durable about a sender.

        Earlier this adjusted keyword weights and then wrote the VIP and mute lists
        back unchanged, which taught nothing. The sender profile is the real memory:
        it is consulted by stage 1 for free and stops stage 2 ever asking again.
        """
        item = self._store.item(item_id)
        if item is None or not item.sender_addr:
            return

        weights = dict(self._store.get_setting("keyword_weights") or {})
        step = -1 if kind in ("not_important", "less_urgent") else 1
        for word in _subject_words(item.subject):
            weights[word] = weights.get(word, 0) + step
        self._store.set_setting("keyword_weights", weights)

        if kind == "not_important":
            self._store.set_sender_profile(
                item.sender_addr, is_automated=True, expects_reply=False,
                importance_hint="low", reason="you marked their mail not important",
                decided_by="click", pinned=True)
        elif kind == "more_important":
            self._store.set_sender_profile(
                item.sender_addr, is_automated=False, expects_reply=True,
                importance_hint="high", reason="you raised their importance",
                decided_by="click", pinned=True)
        # more_urgent / less_urgent say something about this message's timing, not
        # about the sender, so they teach the keyword weights and nothing else.

    def forget_sender(self, addr: str) -> dict:
        """Undo what a click or the classifier learned about one sender."""
        self._store.forget_sender(addr)
        self._reflag()
        return {"senders": self._store.sender_profiles()}

    def learned_senders(self) -> list[dict]:
        rows = list(self._store.sender_profiles().values())
        rows.sort(key=lambda r: (not r["pinned"], r["addr"]))
        return rows

    # ── chat ────────────────────────────────────────────────────────────────

    def chat_send(self, item_id: str, text: str) -> dict:
        """Streams a reply into the drawer."""
        self._store.add_chat(item_id, "user", text)
        threading.Thread(target=self._stream_reply, args=(item_id, text),
                         daemon=True, name="helm-chat").start()
        return {"streaming": True}

    def _stream_reply(self, item_id: str, prompt: str) -> None:
        with self._chat_lock:
            def emit(chunk: str) -> None:
                self._push(f"helm.onChatToken({json.dumps(item_id)}, {json.dumps(chunk)})")

            reply, card = self._answer(item_id, prompt, emit)
            self._store.add_chat(item_id, "assistant", reply)
            self._push(f"helm.onChatDone({json.dumps(item_id)}, {json.dumps(card)})")

    def _answer(self, item_id: str, prompt: str, emit) -> tuple[str, dict | None]:
        """Real connector where one is built for this item type, scripted otherwise.

        Returns the reply text and an approval card, if the intent would write.
        The fallback says which phase makes it real rather than passing a canned
        answer off as a reasoned one.
        """
        item = self._store.item(item_id)
        src = self._sources.get(item.source) if item else None
        wants_write = any(w in prompt.lower() for w in _WRITE_INTENTS)

        # A mail draft IS the answer: the model writes it once and the card shows the
        # same text, rather than generating prose and then a second, different draft.
        if item is not None and wants_write and isinstance(src, (MailSource, SlackSource)):
            return self._draft_reply(item_id, src, prompt, emit)

        if item is not None and isinstance(src, CalendarSource) and "prep" in prompt.lower():
            self._store.log_action("prep_brief", item_id, "calendar", prompt[:200], "started")
            try:
                reply = src.prep_brief(item, on_token=emit)
                self._store.log_action("prep_brief", item_id, "calendar",
                                       reply[:400], "done")
                return reply, None
            except cli.ClaudeUnavailable as e:
                msg = (f"Could not reach the connector: {e}\n\n"
                       "The meeting details above are from the last successful sweep.")
                emit(msg)
                self._store.log_action("prep_brief", item_id, "calendar", str(e)[:200],
                                       "failed")
                return msg, None

        reply = DemoSource.scripted_reply(prompt)
        if not self._demo:
            reply = (f"{reply}\n\n[Not a real answer: chat for {item.source if item else 'this'} "
                     "items is scripted until its phase lands. Live right now: mail "
                     "and Slack drafts, and calendar prep briefs.]")
        for token in _tokens(reply):
            emit(token)
            time.sleep(0.012)
        return reply, self._approval_card(item_id, prompt)

    def _draft_reply(self, item_id: str, src, prompt: str,
                     emit) -> tuple[str, dict | None]:
        """Write a reply through the connector. Nothing is sent, and nothing can be."""
        channel = src.name
        self._store.log_action("draft", item_id, channel, prompt[:200], "started")
        emit("Drafting in your voice…\n\n")
        try:
            draft = src.draft(item_id, prompt)
        except cli.ClaudeUnavailable as e:
            msg = f"Could not reach the connector: {e}"
            emit(msg)
            self._store.log_action("draft", item_id, channel, str(e)[:200], "failed")
            return msg, None
        except NotImplementedError as e:
            emit(str(e))
            return str(e), None

        self._store.add_draft(draft)
        self._store.log_action("draft", item_id, "mail", draft.body, "drafted")
        shown = f"{draft.subject}\n\n{draft.body}"
        emit(shown)
        return shown, {
            "draft_id": draft.id, "channel": "mail", "to": draft.to_addr,
            "subject": draft.subject, "body": draft.body, "risk": "send",
        }

    def _approval_card(self, item_id: str, prompt: str) -> dict | None:
        """Only intents that would write anything produce a card."""
        writes = ("draft reply", "reply", "propose new time", "post summary")
        if not any(w in prompt.lower() for w in writes):
            return None
        item = self._store.item(item_id)
        source = item.source if item else "mail"
        try:
            draft = self._sources[source].draft(item_id, prompt)
        except NotImplementedError:
            # A read-only source offers no card at all, rather than a card that
            # would fail on click.
            return None
        self._store.add_draft(draft)
        self._store.log_action("draft", item_id, source, draft.body, "drafted")
        return {
            "draft_id": draft.id, "channel": draft.channel, "to": draft.to_addr,
            "subject": draft.subject, "body": draft.body,
            "risk": "send",  # send | post | irreversible
        }

    def quick_action(self, item_id: str, chip: str) -> dict:
        return self.chat_send(item_id, chip)

    # ── the one place a send can begin ──────────────────────────────────────

    def approve_send(self, draft_id: str) -> dict:
        """Called from the approval card's click handler. The only mint site."""
        draft = self._store.draft(draft_id)
        if draft is None:
            return {"error": "unknown draft"}
        token = ApprovalToken.mint_from_click(draft_id)
        source = self._sources.get(draft.channel if draft.channel in self._sources else "mail")
        try:
            result = source.send(draft_id, token)
        except (SendRefused, NotImplementedError) as e:
            self._store.log_action("send", draft.to_addr, draft.channel, draft.body,
                                   "failed", approved_by="click")
            self._bus.publish("action_logged", {"draft_id": draft_id})
            return {"error": str(e) or "this source cannot send yet"}
        self._store.log_action("send", draft.to_addr, draft.channel, draft.body,
                              "sent" if result.get("ok") else "failed", approved_by="click")
        self._bus.publish("action_logged", {"draft_id": draft_id})
        return result

    def rebuild_voice(self) -> dict:
        """Read a few sent messages and relearn the writing style. Its own call."""
        src = self._sources.get("mail")
        if not hasattr(src, "refresh_voice"):
            return {"error": "the mail source does not learn a voice in this mode"}
        profile = src.refresh_voice(force=True)
        self._bus.publish("source_status", {"source": "mail", "voice": True})
        return {"profile": profile, "described": describe_voice(profile)}

    def action_log(self) -> list[dict]:
        return self._store.action_log(200)

    # ── shortcuts ───────────────────────────────────────────────────────────

    def shortcuts(self) -> list[dict]:
        return self._store.shortcuts()

    def add_shortcut(self, label: str, url: str, group_name: str = "Work") -> dict:
        try:
            sid = self._store.add_shortcut(label, url, group_name)
        except ValueError as e:
            return {"error": str(e)}
        return {"id": sid, "shortcuts": self._store.shortcuts()}

    def update_shortcut(self, shortcut_id: int, fields: dict) -> dict:
        try:
            self._store.update_shortcut(int(shortcut_id), **fields)
        except ValueError as e:
            return {"error": str(e)}
        return {"shortcuts": self._store.shortcuts()}

    def delete_shortcut(self, shortcut_id: int) -> dict:
        self._store.delete_shortcut(int(shortcut_id))
        return {"shortcuts": self._store.shortcuts()}

    def open_shortcut(self, url: str) -> dict:
        # http/https only. A shortcut is never a file and never a command.
        if not str(url).lower().startswith(("http://", "https://")):
            return {"error": "only http and https links can be opened"}
        webbrowser.open(url)
        self._store.log_action("open_link", url, "local", "", "done")
        return {"ok": True}

    def export_shortcuts(self) -> str:
        return json.dumps(self._store.shortcuts(), indent=2)

    def import_shortcuts(self, payload: str) -> dict:
        try:
            rows = json.loads(payload)
        except json.JSONDecodeError as e:
            return {"error": f"not valid JSON: {e}"}
        added, skipped = 0, 0
        for row in rows if isinstance(rows, list) else []:
            try:
                self._store.add_shortcut(row.get("label", "untitled"), row.get("url", ""),
                                        row.get("group_name", "Imported"))
                added += 1
            except (ValueError, AttributeError):
                skipped += 1
        return {"added": added, "skipped": skipped, "shortcuts": self._store.shortcuts()}

    # ── settings ────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        return self._store.all_settings()

    def set_setting(self, key: str, value) -> dict:
        self._store.set_setting(key, value)
        if key in ("vips", "managers", "muted_senders", "muted_channels",
                   "internal_domains", "keyword_weights"):
            self._reflag()
        return self._store.all_settings()

    def _reflag(self) -> None:
        cfg = self._settings_obj()
        for item in self._store.items():
            existing = self._store.flag(item.id)
            self._store.set_flag(flagmod.classify(item, cfg, existing=existing))
        self._bus.publish("flag_changed", {"all": True})

    def reset_demo(self) -> dict:
        self._store.reset_demo()
        self.sync()
        return {"ok": True, "items": self.list_items()}

    # ── meeting alerts ──────────────────────────────────────────────────────

    def watch_meetings(self, tick: float = 20.0) -> None:
        """Fires meeting_in_5 off a local timer: exact, free, and right offline.

        Only meetings are allowed to interrupt on a timer. Everything else waits for
        its sweep, because a notification HELM cannot be sure of is worse than none.
        """
        warned: set[str] = set()
        while self._scheduler is None or not self._scheduler._stop.is_set():
            now = datetime.now().astimezone()
            # A snooze that never ends is a delete. This tick is the only thing that
            # brings one back, and it is local and free.
            woken = self._store.wake_snoozed()
            if woken:
                self._bus.publish("item_changed", {"woken": woken})
            try:
                self._run_due_summaries()
            except Exception as e:      # a bad summary must not stop the countdown
                print(f"[helm] summary generation failed: {e}")
            for item in self._store.items(source="calendar"):
                if item.id in warned or not item.starts_at:
                    continue
                try:
                    starts = datetime.fromisoformat(item.starts_at)
                except ValueError:
                    continue
                minutes = (starts - now).total_seconds() / 60
                if 0 < minutes <= 5:
                    warned.add(item.id)
                    self._bus.publish("meeting_in_5", {
                        "item_id": item.id, "subject": item.subject,
                        "starts_at": item.starts_at, "join_url": item.join_url})
            time.sleep(tick)

    def close(self) -> None:
        for src in self._sources.values():
            if hasattr(src, "close"):
                try:
                    src.close()
                except Exception:
                    pass

    # ── bridge plumbing ─────────────────────────────────────────────────────

    def _push(self, js: str) -> None:
        for win in (self._panel, self._island):
            if win is None:
                continue
            try:
                win.evaluate_js(js)
            except Exception:
                pass

    def wire_bus(self) -> None:
        def relay(event: str):
            def fn(payload: dict) -> None:
                self._push(f"helm.onEvent({json.dumps(event)}, {json.dumps(payload)})")
            return fn
        for event in ("new_email", "slack_mention", "meeting_in_5", "source_status",
                      "flag_changed", "item_changed", "action_logged", "privacy_changed"):
            self._bus.subscribe(event, relay(event))


# Chat intents that would produce something sendable. Module-level, not a class
# attribute: pywebview exposes every public attribute of js_api to JavaScript.
_WRITE_INTENTS = ("draft reply", "reply", "propose new time", "post summary",
                  "follow up", "nudge", "delegate")

def _next_morning(now: datetime | None = None) -> datetime:
    """Snooze lands at 08:00 local, today if it has not happened yet.

    A fixed offset such as "18 hours" puts items back at whatever hour you happened
    to snooze them, which is how a snooze stops meaning anything. Smart snooze
    ("after my 2pm", "when they reply") is a later phase.
    """
    local = (now or datetime.now()).astimezone()
    target = local.replace(hour=8, minute=0, second=0, microsecond=0)
    return target if target > local else target + timedelta(days=1)


_STOPWORDS = {"the", "and", "for", "you", "your", "with", "from", "this", "that", "need",
              "needed", "please", "re", "fwd"}


def _subject_words(subject: str) -> list[str]:
    words = [w.strip(".,:;!?()[]\"'").lower() for w in (subject or "").split()]
    return [w for w in words if len(w) > 3 and w not in _STOPWORDS][:6]


def _tokens(text: str):
    """Word-at-a-time, keeping newlines, so the drawer streams like real output."""
    buf = ""
    for ch in text:
        buf += ch
        if ch in " \n":
            yield buf
            buf = ""
    if buf:
        yield buf


def run(demo: bool = True, db_path: str | None = None) -> None:
    store = Store(db_path)
    bus = EventBus()

    availability = cli.Availability()
    if not demo:
        # ~20 s, so once at launch and never on a hot path.
        print("[helm] checking connectors…")
        availability = cli.probe_availability()
        for which in ("Microsoft 365", "Slack"):
            reason = availability.reason(which)
            print(f"[helm] {which}: {'connected' if not reason else reason}")

    api = Api(store, bus, demo, availability)

    if not store.shortcuts():
        for label, url, group in DEFAULT_SHORTCUTS:
            store.add_shortcut(label, url, group)

    if demo and not store.get_setting("first_run_done"):
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)

    # Live sweeps take 66-97 s each, so launch paints from the last sync and the
    # scheduler fills in. Demo fixtures are instant, so they load up front.
    if demo:
        api.sync()

    api._island = webview.create_window(
        "HELM", str(UI / "island.html"), js_api=api,
        width=ISLAND_W, height=ISLAND_H, x=_island_x(), y=0,
        frameless=True, easy_drag=False, on_top=True, resizable=False,
        transparent=True, background_color="#0C0F14",
    )
    api._panel = webview.create_window(
        "HELM", str(UI / "panel.html"), js_api=api,
        width=PANEL_W, height=PANEL_H, min_size=(760, 520),
        hidden=True, background_color="#0C0F14",
    )
    # A database written before phase 7 has rows but no index. One rebuild, local.
    if store.search_available and store.search_size() == 0 and store.items():
        n = store.reindex()
        print(f"[helm] built the search index from {n} existing items")

    api.wire_bus()

    tray, keys = Tray(), Hotkeys()
    keys.on("toggle_panel", api.open_panel)
    keys.on("ask_bar", lambda: (api.open_panel(), api._push("helm.focusAsk()")))
    keys.on("focus_queue", lambda: (api.open_panel(), api._push("helm.go('focus')")))
    keys.on("panic", lambda: api.set_privacy())

    scheduler = Scheduler(
        bus, api.poll, tuple(api._sources),
        quiet=lambda: (store.get_setting("quiet_hours", {}).get("start", "19:00"),
                       store.get_setting("quiet_hours", {}).get("end", "07:00")))
    api._scheduler = scheduler

    def quit_app() -> None:
        scheduler.stop()
        api._privacy.stop()
        keys.stop()
        tray.stop()
        api.close()
        for w in (api._island, api._panel):
            try:
                w.destroy()
            except Exception:
                pass

    def start_background() -> None:
        tray.start(on_open=api.open_panel,
                   on_island=lambda: api._island.show(),
                   on_settings=lambda: (api.open_panel(), api._push("helm.go('settings')")),
                   on_quit=quit_app)
        keys.start()
        api._privacy.start()
        # Runs in demo mode too: it is local, free, and the only thing that ends a
        # snooze.
        threading.Thread(target=api.watch_meetings, daemon=True,
                         name="helm-meetings").start()
        if not demo:
            scheduler.start()
        # The guided tour lives in the panel, so a first run that leaves the panel
        # hidden shows the user nothing at all — which is exactly what happened.
        if not store.get_setting("first_run_done"):
            api.open_panel()

    webview.start(start_background, private_mode=False,
                  storage_path=str(Store(db_path).path.parent / "webview"),
                  debug=False)
    quit_app()


def _island_x() -> int:
    """Centre on the primary screen; multi-monitor placement is a settings concern."""
    try:
        screen = webview.screens[0]
        return max(0, int(screen.width / 2 - ISLAND_W / 2))
    except Exception:
        return 600
