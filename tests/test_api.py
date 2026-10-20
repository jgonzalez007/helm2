"""Headless check of the bridge API: no window, no network.

    python -m tests.test_api
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from helm.app import Api
from helm.bus import EventBus
from helm.connectors.demo import DEMO_SETTINGS
from helm.store import Store

passed = 0


def check(label: str, cond: bool = True, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"FAILED: {label}" + (f" — {detail}" if detail else ""))


def fresh_api(tmp: Path) -> Api:
    store = Store(tmp / "helm.db")
    for key, value in DEMO_SETTINGS.items():
        store.set_setting(key, value)
    store.add_shortcut("Jira", "https://example.atlassian.net", "Work")
    api = Api(store, EventBus(), demo=True)
    api.wire_bus()
    api.sync()
    return api


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = Path(d)
        api = fresh_api(tmp)

        print("bootstrap")
        boot = api.bootstrap()
        for key in ("settings", "sources", "items", "shortcuts", "hotkeys", "action_log"):
            check(f"bootstrap has {key}", key in boot)
        check("bootstrap is JSON-serializable", bool(json.dumps(boot)))
        check("every source reported a status", len(boot["sources"]) == 3,
              str(boot["sources"]))
        check("items came back", len(boot["items"]) >= 10, str(len(boot["items"])))

        print("ordering")
        quads = [r["flag"]["quadrant"] for r in boot["items"]]
        check("important+urgent sorts first", quads[0] == "important_urgent", quads[0])
        check("'neither' sorts last", quads[-1] == "neither", quads[-1])

        print("item detail")
        first = boot["items"][0]["item"]["id"]
        detail = api.item_detail(first)
        for key in ("item", "flag", "thread", "chat", "attached"):
            check(f"detail has {key}", key in detail)
        check("the drawer states what it attached", len(detail["attached"]) == 4)
        check("it names something it deliberately excludes",
              any(not a["included"] for a in detail["attached"]))
        check("an unknown id fails cleanly", "error" in api.item_detail("nope"))

        print("flag override is pinned and survives a resync")
        api.flag_override(first, "not_important")
        check("override applied", api._store.flag(first).importance == "low")
        check("override pinned", api._store.flag(first).pinned is True)
        api.sync()
        check("a full resync leaves it alone", api._store.flag(first).importance == "low")

        print("state changes")
        api.set_item_state(first, "done")
        check("state persisted", api._store.item(first).state == "done")
        check("state change is logged", api._store.action_log()[0]["verb"] == "state")
        check("a state change is not an approved send",
              api._store.action_log()[0]["approved_by"] == "")

        print("chat drafts, then waits for a click")
        api.chat_send(first, "draft reply")
        for _ in range(100):
            if any(m["role"] == "assistant" for m in api._store.chat(first)):
                break
            time.sleep(0.05)
        chat = api._store.chat(first)
        check("the user turn is stored", chat[0]["role"] == "user")
        check("the reply is stored", any(m["role"] == "assistant" for m in chat))

        drafts = [a for a in api._store.action_log() if a["verb"] == "draft"]
        check("drafting is logged", bool(drafts))
        check("a draft is not an approval", drafts[0]["approved_by"] == "")
        check("nothing has been sent",
              not [a for a in api._store.action_log() if a["verb"] == "send"])

        print("approve_send is the only path to a send")
        row = api._store._conn().execute("SELECT id FROM draft LIMIT 1").fetchone()
        result = api.approve_send(row["id"])
        check("send succeeded through the gate", result.get("ok") is True)
        check("demo mode sent nothing", result.get("demo") is True)
        sends = [a for a in api._store.action_log() if a["verb"] == "send"]
        check("the send is logged", bool(sends))
        check("logged as approved by click", sends[0]["approved_by"] == "click")
        # The property is that the log holds the exact body that was approved, not
        # that it contains any particular word — the drafts are per-item now.
        draft_row = api._store.draft(row["id"])
        check("the exact content is recorded",
              sends[0]["payload"] == draft_row.body and bool(draft_row.body),
              f"{sends[0]['payload'][:60]!r} vs {draft_row.body[:60]!r}")
        check("and so is the exact recipient", sends[0]["target"] == draft_row.to_addr)
        check("an unknown draft is refused", "error" in api.approve_send("no-such-draft"))

        print("shortcuts")
        bad = api.add_shortcut("Danger", "file:///C:/Windows/System32/cmd.exe")
        check("a file URL is refused", "error" in bad, str(bad))
        check("opening a non-http URL is refused",
              "error" in api.open_shortcut("javascript:alert(1)"))
        ok = api.add_shortcut("Docs", "https://example.com/docs", "Reference")
        check("an https link is accepted", "id" in ok)
        exported = api.export_shortcuts()
        check("export is JSON", isinstance(json.loads(exported), list))
        reimport = api.import_shortcuts(exported)
        check("re-import adds them back", reimport["added"] >= 2, str(reimport))
        check("import of junk fails cleanly", "error" in api.import_shortcuts("{nope"))

        print("settings edits reflag everything unpinned")
        before = api._store.flag("demo-mail-4").importance
        api.set_setting("vips", ["no-reply@sailpoint.com"])
        after = api._store.flag("demo-mail-4").importance
        check("a bulk address stays low even as a VIP", (before, after) == ("low", "low"),
              f"{before} -> {after}")
        api.set_setting("managers", ["atlee.chalwell@sailpoint.com"])
        check("a new manager raises their item",
              api._store.flag("demo-slack-2").importance == "high")
        check("the pinned override still stands",
              api._store.flag(first).importance == "low")

        print("the JavaScript surface is only what we intended")
        # pywebview builds its JS proxy by walking every public attribute of the
        # js_api object, recursing into objects it finds. A public `self.store`
        # would hand JS `api.store.log_action(..., approved_by='click')` and make
        # the approval gate decorative.
        surface = {n for n in dir(api) if not n.startswith("_")}
        expected = {
            "open_panel", "hide_panel", "resize_island", "sync", "bootstrap",
            "list_items", "item_detail", "set_item_state", "flag_override",
            "chat_send", "quick_action", "approve_send", "action_log",
            "shortcuts", "add_shortcut", "update_shortcut", "delete_shortcut",
            "open_shortcut", "export_shortcuts", "import_shortcuts",
            "get_settings", "set_setting", "reset_demo", "wire_bus",
            "poll", "resync", "schedule_info", "watch_meetings", "close",
            "rebuild_voice", "classify_ambiguous", "forget_sender",
            "learned_senders",
            "privacy_state", "set_privacy", "may_interrupt",
        }
        check("no unexpected names are reachable from JS", surface == expected,
              f"extra={sorted(surface - expected)} missing={sorted(expected - surface)}")
        for leaked in ("store", "bus", "sources", "island", "panel", "privacy"):
            check(f"'{leaked}' is not on the JS surface", leaked not in surface)

        print("demo reset")
        reset = api.reset_demo()
        check("reset returns fresh items", len(reset["items"]) >= 10)
        check("the action log is cleared", api._store.action_log() == [])
        check("shortcuts survive a reset", len(api._store.shortcuts()) >= 2)

    print(f"\n{passed} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
