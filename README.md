# HELM 2.0

A local-first desktop command center for one person's work day: Outlook mail and
calendar, Slack, two-axis importance/urgency flags, per-item chat, daily summaries,
and a shortcuts panel. One Python codebase, Windows and macOS, **no npm**.

Phases 1 and 2 of 11 are complete: the whole UI on fixtures, plus a real read-only
calendar through the connectors your organization already approved.

## Run it

Windows:

```bash
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py --demo
```

macOS / Linux:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python run.py --demo
```

A thin island appears at the top-centre of the screen. Hover it to peek, hover again
to expand, click it to open the panel. The panel is also on `Ctrl+Shift+H`.

Demo mode needs no accounts, no tokens and no network.

### Live mode

```bash
.\.venv\Scripts\python.exe run.py --check
```

That reports whether the `claude` CLI and your connectors are reachable, then exits.
If Microsoft 365 says `connected`:

```bash
.\.venv\Scripts\python.exe run.py --live
```

Your real calendar, read-only. Mail and Slack show as `unavailable` and name the
phase that builds them — live mode never pads a gap with fixtures.

**What live mode costs.** Every sweep is a model turn. The cadence is 5 minutes for
calendar during work hours, 30 minutes outside them, paused during quiet hours.
Settings shows the real cadence, the next sweep and the measured round-trip.

## Check it

```bash
.\.venv\Scripts\python.exe -m tests.test_flags
```

```bash
.\.venv\Scripts\python.exe -m tests.test_api
```

```bash
.\.venv\Scripts\python.exe -m tests.test_phase2
```

```bash
.\.venv\Scripts\python.exe -m tests.smoke_window
```

```bash
.\.venv\Scripts\python.exe -m tests.live_calendar --prep
```

`test_flags` (64 checks) covers the flag rules, including that a message cannot
influence its own flag. `test_api` (54 checks) covers the bridge, the approval gate
and the JS surface. `test_phase2` (104 checks) covers connector parsing, hostile
input, polling cadence and the tool allowlists. `smoke_window` opens the real
windows, drives the UI and reads the rendered DOM back — it needs a display.
`live_calendar` does one real sweep against your calendar — it needs the connectors
and takes a few minutes.

## What phase 1 gives you

- Island with hidden/peek/compact states, live counts and an exact meeting countdown
- Panel: Today, Focus, Inbox, Slack, Calendar, Actions, Recap, Search, Shortcuts, Settings
- Fixtures covering all four flag quadrants, plus a phishing-style "urgent" external
  message whose flags stay quiet on purpose
- Two-axis flag badges with why-tooltips and one-click overrides that pin forever
- Focus queue, one item at a time, `j`/`k`/`r`/`e`/`s`/`d`
- Per-item chat with streaming replies (scripted in phase 1) and approval cards
- 30-second delay-send with undo, and a viewable action log
- Shortcuts with `Alt+1…9`, add/delete, JSON export/import, http(s) only
- Guided first run, light/dark/system, compact/comfortable, reduced motion,
  professional mode, privacy blur with a panic hotkey
- Tray icon and global hotkeys, both degrading to no-ops where unsupported

## Keyboard

Press `?` in the panel for the full list.

| Key | Does |
| --- | --- |
| `Ctrl+Shift+H` | show the panel from anywhere |
| `Ctrl/⌘+K` | ask bar |
| `Ctrl+Shift+F` | focus queue |
| `Ctrl+Shift+\` | panic — blur everything |
| `Alt+1…9` | open shortcut 1 to 9 |
| `j` `k` | move through the focus queue |
| `r` `e` `s` `d` | reply, done, snooze, delegate |
| `?` | keyboard overlay |

## What phase 2 adds

- **Real calendar, read-only**, through the approved Microsoft 365 connector
- **Prep briefs** that actually call the connector, streaming into the chat drawer
- **A polling scheduler**: 5 min for calendar in work hours, 30 min outside,
  paused in quiet hours, exponential backoff on failure, resync after sleep/wake
- **`meeting_in_5`** off a local timer — the only thing allowed to interrupt you
- **Conflict detection** computed locally, so it is right even offline
- **Honest status**: a polled source is never called "live"; the age is always stated

## Known limits

- **Mail and Slack chat is still scripted.** Prep briefs on calendar events are the
  only live chat path. Mail is phase 3, Slack phase 4; the drawer says so rather
  than passing a canned answer off as a reasoned one.
- **Nothing can be sent.** Every source is read-only. The approval card, the
  30-second undo and the action log all work, but `send()` is wired in phase 10.
- **Calendar times assume your mailbox zone matches this machine.** If it does not,
  Settings shows a warning rather than silently shifting your meetings.
- **Join links are missing for most meetings.** The calendar search result carries one
  only sometimes. Fetching the rest needs a per-event read, which would roughly triple
  a 144-second sweep, so the Join button appears only where a link really came back.
- **A sweep returns at most 25 events** over the next two days. On a dense week that is
  a real ceiling; the window shrinks before the limit bites.
- **Search filters what is loaded.** SQLite FTS5 is phase 7.
- **Commitments are illustrative.** Extraction is phase 8.
- **Privacy mode is manual.** The panic hotkey and blur work; automatic
  screen-share and meeting-foreground detection is phase 6.
- **macOS is unverified.** Written from the docs, never run on a Mac.

## Why no npm, and why pywebview is pinned

See [CLAUDE.md](CLAUDE.md). Short version: this machine has no Node toolchain and must
not acquire one, and pywebview 6.2.1 fails WebView2 initialisation here with
`E_ABORT 0x80004004` while 5.4 works.
