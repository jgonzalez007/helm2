"""OutlookDesktopSource — the seconds-latency mail path, where it exists.

On this machine it does not, and this file's job is to say so precisely rather than
to keep trying. Measured 2026-10-19:

* `Outlook.Application` is registered, so a naive COM probe looks promising.
* `GetActiveObject` returns `Operation unavailable`: nothing is running.
* There is no MAPI profile under `HKCU\\...\\Outlook\\Profiles` and no `.ost`, so
  classic Outlook was never configured.
* `olk.exe` is running: the user is on new Outlook, which exposes no COM API at all.

`Dispatch("Outlook.Application")` would *launch* classic Outlook rather than
reporting a problem, which on a machine with no profile means a configuration wizard
appearing out of nowhere. So this never dispatches. It attaches to a running
instance or reports unavailable, and `detect()` explains which of the four states
the machine is in.

macOS would use AppleScript (`tell application "Microsoft Outlook"`). That path is
written but unverified — there is no Mac in this project yet.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass

from ..bus import EventBus
from ..models import ApprovalToken, Draft, Item, SourceStatus, Thread
from .base import SendRefused


@dataclass
class Detection:
    available: bool = False
    reason: str = ""
    detail: str = ""
    # What the user would have to change, when anything could change it.
    remedy: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _windows_detect() -> Detection:
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return Detection(reason="pywin32 is not installed",
                         remedy="pip install pywin32 into the venv")

    try:
        import winreg
    except ImportError:          # pragma: no cover - winreg ships with CPython
        winreg = None

    profiles = 0
    if winreg is not None:
        for hive_path in (
            r"Software\Microsoft\Office\16.0\Outlook\Profiles",
            r"Software\Microsoft\Windows NT\CurrentVersion\Windows Messaging Subsystem\Profiles",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, hive_path) as key:
                    profiles += winreg.QueryInfoKey(key)[0]
            except OSError:
                continue

    if profiles == 0:
        return Detection(
            reason="classic Outlook has no mail profile on this machine",
            detail="no profile key and no .ost file; new Outlook (olk.exe) is in use",
            remedy="nothing to fix — new Outlook exposes no COM API, so this source "
                   "cannot work here. Mail comes from the approved connector instead.")

    pythoncom.CoInitialize()
    try:
        win32com.client.GetActiveObject("Outlook.Application")
    except Exception as e:
        return Detection(
            reason="classic Outlook is not running",
            detail=f"{type(e).__name__}: {e}",
            remedy="start classic Outlook and sweep again. HELM never launches it "
                   "itself: on a machine with no profile that opens a setup wizard.")
    return Detection(available=True, reason="attached to a running classic Outlook")


def _macos_detect() -> Detection:
    # shortcut: not verified — there is no Mac in this project. Written from the
    # AppleScript dictionary; upgrade to a real probe the first time it runs on one.
    import shutil
    if shutil.which("osascript") is None:
        return Detection(reason="osascript is missing", remedy="this is not a Mac")
    return Detection(
        reason="the macOS path has never been run",
        detail="AppleScript against Microsoft Outlook is written but unverified",
        remedy="run tests/test_phase9.py on a Mac and replace this with a real probe")


def detect() -> Detection:
    if sys.platform == "win32":
        return _windows_detect()
    if sys.platform == "darwin":
        return _macos_detect()
    return Detection(reason="Outlook desktop automation is Windows and macOS only",
                     detail=f"this is {sys.platform}")


class OutlookDesktopSource:
    """Satisfies the Connector protocol and tells the truth about itself.

    It never fabricates rows and never empties a list. When unavailable it returns
    nothing and its pill goes grey with the reason attached, which is the whole
    point of building it on a machine where it cannot work.
    """

    name = "outlook_desktop"
    prefetch_threads = True
    # Never drives two-way sync: see Api.poll.
    retires_missing = False

    def __init__(self) -> None:
        self.detection = detect()

    def status(self) -> SourceStatus:
        d = self.detection
        return SourceStatus(
            source=self.name,
            health="live" if d.available else "unavailable",
            last_error="" if d.available else f"{d.reason}. {d.remedy}".strip(),
        )

    def list_new(self, since: str = "") -> list[Item]:
        # Re-detect: classic Outlook may have been started since the last sweep.
        self.detection = detect()
        if not self.detection.available:
            return []
        raise NotImplementedError(
            "reading from a live classic Outlook is unimplemented: no machine in "
            "this project has one, so it would be code nobody has ever run")

    def get_thread(self, item_id: str) -> Thread:
        return Thread(item_id=item_id, messages=[])

    def draft(self, item_id: str, intent: str) -> Draft:
        raise NotImplementedError(f"{self.name}: {self.detection.reason}")

    def send(self, draft_id: str, approval: ApprovalToken,
             draft: Draft | None = None) -> dict:
        raise SendRefused(f"{self.name}: {self.detection.reason}")

    def subscribe(self, bus: EventBus) -> None:
        return None
