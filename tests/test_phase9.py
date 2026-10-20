"""Phase 9: the realtime paths.

Reduced in scope on purpose. Classic Outlook COM cannot work on this machine, so
`OutlookDesktopSource` exists to say exactly why, and the drop folder is the only
sub-minute mail path left. These checks need no window, no network and no Outlook.

    python -m tests.test_phase9
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from helm import scheduler as sched
from helm.app import Api
from helm.bus import EventBus
from helm.connectors.base import SendRefused
from helm.connectors.demo import DEMO_SETTINGS
from helm.connectors.dropfolder import (MAX_FILE_BYTES, MAX_FILES_PER_SWEEP,
                                        DropFolderSource)
from helm.connectors.outlook_desktop import OutlookDesktopSource, detect
from helm.models import Item
from helm.store import Store

passed = 0


def check(label: str, cond: bool = True, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"FAILED: {label}" + (f" — {detail}" if detail else ""))


def write(folder: Path, name: str, payload) -> Path:
    path = folder / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                    encoding="utf-8")
    return path


# ── Outlook desktop: say why, do not keep trying ────────────────────────────

def test_outlook_detection_is_specific() -> None:
    print("the Outlook detector explains itself")
    d = detect()
    check("it reaches a verdict", isinstance(d.available, bool))
    check("and gives a reason", bool(d.reason), str(d.to_dict()))

    if sys.platform == "win32":
        # Measured on this machine: no MAPI profile, new Outlook in use.
        check("it is unavailable here", d.available is False, d.reason)
        check("the reason names the profile or a stopped Outlook",
              "profile" in d.reason or "not running" in d.reason, d.reason)
        check("and says what would change it", bool(d.remedy), str(d.to_dict()))
    else:
        check("a non-Windows machine says so", d.available is False)


def test_outlook_source_never_fabricates_or_empties() -> None:
    print("the Outlook source tells the truth and returns nothing")
    src = OutlookDesktopSource()
    st = src.status()
    check("status is unavailable, not stale",
          st.health in ("unavailable", "live"), st.health)
    if st.health == "unavailable":
        check("the error carries the reason and the remedy", bool(st.last_error),
              st.last_error)
    check("it returns no rows rather than invented ones", src.list_new() == [])
    check("it never drives two-way sync", src.retires_missing is False)

    try:
        src.draft("x", "draft reply")
    except NotImplementedError as e:
        check("drafting refuses with the reason", bool(str(e)))
    else:
        raise AssertionError("FAILED: the Outlook source offered a draft")

    try:
        src.send("x", None)
    except SendRefused:
        check("and sending refuses outright")
    else:
        raise AssertionError("FAILED: the Outlook source accepted a send")


def test_it_never_launches_outlook() -> None:
    print("it attaches to a running Outlook, and never starts one")
    source = (Path("helm/connectors/outlook_desktop.py")).read_text(encoding="utf-8")
    check("GetActiveObject is used", "GetActiveObject" in source)
    # Dispatch() would launch classic Outlook, which on a machine with no profile
    # opens a setup wizard out of nowhere. The docstring names it to explain why it
    # is absent, so this looks for a call rather than the word.
    calls = [ln for ln in source.splitlines()
             if ".Dispatch(" in ln and not ln.strip().startswith(("#", "*"))]
    check("Dispatch is never called", not calls, str(calls))
    check("and the file says why", "setup wizard" in source or "wizard" in source)


# ── the drop folder ─────────────────────────────────────────────────────────

def test_folder_absent_is_unavailable_not_empty() -> None:
    print("a missing folder is unavailable, with the setup instructions")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = DropFolderSource(Path(d) / "nope")
        st = src.status()
        check("unavailable", st.health == "unavailable", st.health)
        check("the error names the folder", "nope" in st.last_error, st.last_error)
        check("and explains the Power Automate flow",
              "Power Automate" in st.last_error, st.last_error)
        check("it returns nothing", src.list_new() == [])

    none = DropFolderSource(None)
    none.folder = None
    check("no folder configured is also unavailable",
          none.status().health == "unavailable")


def test_a_good_file_becomes_an_item() -> None:
    print("a well-formed file becomes one item")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        folder = Path(d)
        write(folder, "a.json", {
            "id": "AAMk123", "subject": "Q4 numbers",
            "from": "Scott.Chen@SailPoint.com", "fromName": "Scott Chen",
            "preview": "Need these before Thursday.",
            "received": "2026-10-19T18:39:52.000Z", "toMe": True,
            "conversationId": "conv-1",
        })
        src = DropFolderSource(folder, ("sailpoint.com",))
        items = src.list_new()
        check("one item", len(items) == 1, str(len(items)))

        item = items[0]
        check("the id is namespaced", item.id == "drop-AAMk123", item.id)
        check("the subject survives", item.subject == "Q4 numbers")
        check("the address is lowercased",
              item.sender_addr == "scott.chen@sailpoint.com", item.sender_addr)
        check("an internal sender is not external", item.sender_external is False)
        check("the UTC instant was converted, not copied",
              item.received_at and not item.received_at.endswith("Z"), item.received_at)
        check("the conversation is kept", item.thread_id == "conv-1")
        check("it is filed as mail so it lands in the inbox", item.source == "mail")

        check("the file was moved out of the way",
              not (folder / "a.json").exists())
        check("and kept, not deleted", (folder / "processed" / "a.json").exists())
        check("so a second sweep returns nothing", src.list_new() == [])


def test_external_sender_is_marked() -> None:
    print("an outside address is marked external")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        folder = Path(d)
        write(folder, "a.json", {"id": "1", "subject": "Hi",
                                 "from": "someone@helpdesk-verify.net"})
        item = DropFolderSource(folder, ("sailpoint.com",)).list_new()[0]
        check("external", item.sender_external is True)


def test_hostile_files_are_rejected_not_trusted() -> None:
    print("everything in the folder is hostile input")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        folder = Path(d)
        hostile = {
            "broken.json": "{not json",
            "empty.json": "",
            "array.json": "[1,2,3]",
            "string.json": '"just a string"',
            "null.json": "null",
            "nofields.json": "{}",
            "numbers.json": '{"id": 12, "subject": {"a": 1}, "from": ["x"], "toMe": "yes"}',
            "nested.json": '{"id": {"deep": {"deeper": 1}}, "subject": "ok"}',
        }
        for name, body in hostile.items():
            write(folder, name, body)
        # A file far over the cap must be rejected without being parsed.
        write(folder, "huge.json", '{"subject": "' + "x" * (MAX_FILE_BYTES + 10) + '"}')

        src = DropFolderSource(folder, ("sailpoint.com",))
        items = src.list_new()

        for item in items:
            check(f"{item.id}: subject is a bounded string",
                  isinstance(item.subject, str) and len(item.subject) <= 400)
            check(f"{item.id}: address is a string",
                  isinstance(item.sender_addr, str))
            check(f"{item.id}: toMe is a real bool",
                  isinstance(item.to_me_direct, bool))

        check("the unparseable ones were rejected", src.state.rejected >= 6,
              f"rejected={src.state.rejected} accepted={[i.id for i in items]}")
        check("rejected files are kept for inspection",
              (folder / "rejected").exists()
              and any((folder / "rejected").iterdir()))
        check("the oversized file never became an item",
              not any("x" * 500 in i.subject for i in items))
        check("nothing crashed the sweep", isinstance(items, list))


def test_a_sweep_is_bounded() -> None:
    print("one bad day in the flow cannot make a sweep unbounded")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        folder = Path(d)
        for i in range(MAX_FILES_PER_SWEEP + 20):
            write(folder, f"{i:04d}.json", {"id": str(i), "subject": f"mail {i}"})
        src = DropFolderSource(folder)
        first = src.list_new()
        check("capped at the limit", len(first) == MAX_FILES_PER_SWEEP, str(len(first)))
        check("the oldest files went first", first[0].id == "drop-0", first[0].id)
        second = src.list_new()
        check("the rest arrive on the next sweep", len(second) == 20, str(len(second)))


def test_drop_folder_is_read_only() -> None:
    print("the drop folder cannot be written through")
    src = DropFolderSource(None)
    try:
        src.draft("x", "draft reply")
    except NotImplementedError as e:
        check("drafting points you at the mail connector", "mail connector" in str(e))
    else:
        raise AssertionError("FAILED: the drop folder offered a draft")
    try:
        src.send("x", None)
    except SendRefused as e:
        check("sending refuses by construction", "read-only" in str(e))
    else:
        raise AssertionError("FAILED: the drop folder accepted a send")


# ── cadence and wiring ──────────────────────────────────────────────────────

def test_local_paths_poll_far_faster() -> None:
    print("a free source polls in seconds, a paid one does not")
    from datetime import datetime
    work = datetime(2026, 10, 19, 10, 0)
    night = datetime(2026, 10, 19, 23, 0)

    check("the drop folder polls every 20 s in work hours",
          sched.interval_for("dropfolder", work) == 20)
    check("and still every minute at night",
          sched.interval_for("dropfolder", night) == 60)
    check("mail stays on its 10-minute cadence",
          sched.interval_for("mail", work) == 600)
    check("because a mail sweep is a model turn and a folder listing is not",
          sched.interval_for("dropfolder", work) < sched.interval_for("mail", work))


def test_a_notification_source_never_retires_the_inbox() -> None:
    print("the drop folder cannot mark your inbox handled")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "helm.db")
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)
        api = Api(store, EventBus(), demo=True)
        api.sync()
        before = len(store.items(states=("needs_me",)))
        check("the demo inbox has rows", before > 0)

        # Drop-folder items are stamped source="mail", so a sweep that retired by
        # source would mark every real mail handled. This is why retires_missing
        # exists.
        folder = Path(d) / "inbox"
        folder.mkdir()
        write(folder, "a.json", {"id": "new1", "subject": "Just arrived"})
        drop = DropFolderSource(folder, ("sailpoint.com",))
        api._sources["dropfolder"] = drop

        api.poll("dropfolder")
        after = len(store.items(states=("needs_me",)))
        check("the new mail was added", store.item("drop-new1") is not None)
        check("and nothing else was retired", after == before + 1,
              f"{before} -> {after}")


def test_realtime_info_reaches_the_ui() -> None:
    print("Settings is told what each path can do")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "helm.db")
        api = Api(store, EventBus(), demo=False)
        info = api.realtime_info()
        for key in ("dropfolder", "outlook_desktop"):
            check(f"{key} is reported", key in info)
            check(f"{key} carries a status", info[key]["status"] is not None)
        check("the drop folder carries setup instructions",
              "Power Automate" in info["dropfolder"]["setup"])
        check("Outlook carries its detection",
              "reason" in (info["outlook_desktop"]["detection"] or {}))
        check("it is JSON-serializable", bool(json.dumps(info)))

        boot = api.bootstrap()
        check("live mode puts it in bootstrap", boot["realtime"] is not None)

        demo = Api(Store(Path(d) / "demo.db"), EventBus(), demo=True)
        check("demo mode does not claim a realtime path",
              demo.bootstrap()["realtime"] is None)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n{passed} checks passed across {len(tests)} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
