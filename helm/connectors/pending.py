"""A source that is designed but not built yet.

Live mode must not quietly serve fixtures next to real data — a panel that mixes
the two is worse than one that admits a gap. These report `unavailable` and say
which phase brings them.
"""

from __future__ import annotations

from ..bus import EventBus
from ..models import ApprovalToken, Draft, Item, SourceStatus, Thread
from .base import SendRefused


class PendingSource:
    def __init__(self, name: str, phase: str) -> None:
        self.name = name
        self.phase = phase

    def status(self) -> SourceStatus:
        return SourceStatus(source=self.name, health="unavailable",
                            last_error=f"{self.name} arrives in {self.phase}")

    def list_new(self, since: str = "") -> list[Item]:
        return []

    def get_thread(self, item_id: str) -> Thread:
        return Thread(item_id=item_id, messages=[])

    def draft(self, item_id: str, intent: str) -> Draft:
        raise NotImplementedError(f"{self.name} arrives in {self.phase}")

    def send(self, draft_id: str, approval: ApprovalToken,
             draft: Draft | None = None) -> dict:
        raise SendRefused(f"{self.name} arrives in {self.phase}")

    def subscribe(self, bus: EventBus) -> None:
        return None
