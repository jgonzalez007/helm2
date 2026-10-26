"""DropFolderSource — the only sub-minute mail path left open on this machine.

A Power Automate flow writes one small JSON file per new mail into a OneDrive
folder; HELM watches that folder locally. No app registration, no token, no API
client: the flow runs as the user inside tooling their organization already
approves, and HELM only ever reads files off the local disk.

Latency is seconds, because a directory listing is free. That is the entire reason
this exists — every other mail path here is a 2-5 minute poll.

Everything in those files is hostile input. Anyone who can write to the folder
decides what they contain, so every field is coerced and bounded by `_clean`, no
string is used as a path or a command, and a file that cannot be parsed is moved
aside rather than retried forever.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..bus import EventBus
from ..models import ApprovalToken, Draft, Item, SourceStatus, Thread
from . import _clean
from .base import SendRefused

# One malformed or enormous file must not stall a sweep or fill memory.
MAX_FILE_BYTES = 256 * 1024
MAX_FILES_PER_SWEEP = 50


@dataclass
class FolderState:
    available: bool = False
    reason: str = ""
    remedy: str = ""
    seen: int = 0
    rejected: int = 0


def default_folder() -> Path | None:
    """The user's commercial OneDrive, if Windows has told us where it is."""
    import os
    base = os.environ.get("OneDriveCommercial") or os.environ.get("OneDrive")
    return Path(base) / "HELM" / "inbox" if base else None


SETUP = (
    "Create a Power Automate flow: trigger 'When a new email arrives (V3)', action "
    "'Create file' in OneDrive, folder HELM/inbox, name utcNow() plus .json, content "
    "a JSON object with id, subject, from, fromName, preview, received, toMe. No app "
    "registration and no token: the flow runs as you."
)


class DropFolderSource:
    """Satisfies the Connector protocol by reading a local folder and nothing else."""

    name = "dropfolder"
    prefetch_threads = True
    # Never drives two-way sync: see Api.poll.
    retires_missing = False

    def __init__(self, folder: Path | str | None = None,
                 internal_domains: tuple[str, ...] = ()) -> None:
        self.folder = Path(folder) if folder else default_folder()
        self.internal_domains = tuple(d.lower() for d in internal_domains)
        self.state = FolderState()

    # ── status ──────────────────────────────────────────────────────────────

    def _check(self) -> FolderState:
        if self.folder is None:
            return FolderState(reason="no OneDrive folder is configured",
                               remedy=SETUP)
        if not self.folder.exists():
            return FolderState(
                reason=f"{self.folder} does not exist",
                remedy=SETUP)
        return FolderState(available=True, reason=f"watching {self.folder}")

    def status(self) -> SourceStatus:
        st = self.state if self.state.reason else self._check()
        return SourceStatus(
            source=self.name,
            health="live" if st.available else "unavailable",
            last_error="" if st.available else f"{st.reason}. {st.remedy}".strip(),
        )

    # ── reading ─────────────────────────────────────────────────────────────

    def list_new(self, since: str = "") -> list[Item]:
        st = self._check()
        self.state = st
        if not st.available:
            return []

        done = self.folder / "processed"
        bad = self.folder / "rejected"
        items: list[Item] = []

        # Sorted so the oldest file wins a duplicate id, and bounded so one bad
        # day in the flow cannot make a sweep unbounded.
        for path in sorted(self.folder.glob("*.json"))[:MAX_FILES_PER_SWEEP]:
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    self._move(path, bad)
                    st.rejected += 1
                    continue
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                # Moved aside, not deleted: a file HELM could not read is evidence.
                self._move(path, bad)
                st.rejected += 1
                continue

            item = self._to_item(payload)
            if item is None:
                self._move(path, bad)
                st.rejected += 1
                continue
            items.append(item)
            self._move(path, done)
            st.seen += 1

        return items

    def _to_item(self, payload) -> Item | None:
        """Coerce one file into an Item, or reject it. Never trusts a field."""
        if not isinstance(payload, dict):
            return None
        ident = _clean._s(payload.get("id"), 200)
        subject = _clean._s(payload.get("subject"))
        if not ident and not subject:
            return None

        addr = _clean._s(payload.get("from"), 200).lower()
        received = _clean.any_time_to_local(_clean._s(payload.get("received"), 60))

        return Item(
            # Namespaced so a flow cannot collide with a connector's own ids.
            id=f"drop-{ident or abs(hash(subject + received))}",
            source="mail",
            kind="email",
            subject=subject,
            sender=_clean._s(payload.get("fromName")) or addr,
            sender_addr=addr,
            sender_external=_clean._is_external(addr, self.internal_domains),
            snippet=_clean._s(payload.get("preview")),
            thread_id=_clean._s(payload.get("conversationId"), 200),
            received_at=received,
            to_me_direct=_clean._bool(payload.get("toMe", True)),
        )

    @staticmethod
    def _move(path: Path, into: Path) -> None:
        try:
            into.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(into / path.name))
        except OSError as e:
            print(f"[helm] could not file {path.name} into {into.name}: {e}")

    # ── the rest of the protocol ────────────────────────────────────────────

    def get_thread(self, item_id: str) -> Thread:
        # A drop file is one message. The thread lives in the mail connector.
        return Thread(item_id=item_id, messages=[])

    def draft(self, item_id: str, intent: str) -> Draft:
        raise NotImplementedError(
            "the drop folder is a notification path, not a mailbox: open the item "
            "to draft through the mail connector")

    def send(self, draft_id: str, approval: ApprovalToken,
             draft: Draft | None = None) -> dict:
        raise SendRefused("the drop folder is read-only by construction")

    def subscribe(self, bus: EventBus) -> None:
        return None
