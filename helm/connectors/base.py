"""The interface every source implements, so a direct API can replace a connector later."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..bus import EventBus
from ..models import ApprovalToken, Draft, Item, SourceStatus, Thread


class SendRefused(Exception):
    """Raised when something tries to send without proof of a human click."""


@runtime_checkable
class Connector(Protocol):
    name: str

    def status(self) -> SourceStatus: ...
    def list_new(self, since: str) -> list[Item]: ...
    def get_thread(self, item_id: str) -> Thread: ...
    def draft(self, item_id: str, intent: str) -> Draft: ...
    def send(self, draft_id: str, approval: ApprovalToken,
             draft: Draft | None = None) -> dict: ...
    def subscribe(self, bus: EventBus) -> None: ...


def require_approval(draft_id: str, approval: ApprovalToken | None) -> None:
    """The one gate every `send()` implementation calls first.

    A token can only come from `ApprovalToken.mint_from_click`, which is called
    from a UI click handler and nowhere else.
    """
    if not isinstance(approval, ApprovalToken):
        raise SendRefused("no approval token: a send requires an explicit click")
    if approval.draft_id != draft_id:
        raise SendRefused("approval token was minted for a different draft")
