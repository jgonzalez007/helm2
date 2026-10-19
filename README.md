# HELM 2.0

A local-first desktop command center for one person's work day: Outlook mail and
calendar, Slack, two-axis importance/urgency flags, per-item chat, daily summaries,
and a shortcuts panel. One Python codebase, Windows and macOS, **no npm**.

Phases 1 to 3 of 11 are complete: the whole UI on fixtures, plus a real read-only
calendar and real mail — triage, needs-reply detection and drafts in your own
voice — through the connectors your organization already approved.

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

Your real calendar and mail, read-only plus drafts. Slack shows as `unavailable`
and names the phase that builds it — live mode never pads a gap with fixtures.

**What live mode costs.** Every sweep is a model turn. Measured here: a calendar
sweep takes 124–144 s and a mail sweep 197–232 s, so both run every 10 minutes in
work hours, 30 minutes outside them, and pause during quiet hours. Settings shows
the real cadence, the next sweep and the measured round-trip.

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
`test_phase3` (154 checks) covers mail parsing, needs-reply, waiting-on and the voice
profile. `live_calendar` and `live_mail` do one real sweep each against your own data
— they need the connectors and take minutes.

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

## What phase 3 adds

- **Real mail**: one sweep reads the inbox and sent items together, because the sent
  list is what makes needs-reply, waiting-on and the voice profile work without a
  second connector call
- **Needs-reply detection** computed from thread structure, never from body text —
  a question mark does not make something need a reply, and "please confirm receipt"
  does not either. Each row states its reason.
- **Waiting-on**: things you sent more than three days ago that nobody answered
- **A voice profile** learned from your own sent mail with plain string matching, no
  model call — greeting, sign-off, typical length — and drafts written to match it
- **Drafts through the connector**, streamed into the chat drawer, every one ending in
  an approval card. `send()` still refuses: that is phase 10.
- **Threads fetched on open, not on sweep.** A thread costs its own connector call;
  pulling 25 per sweep would turn two minutes into half an hour. The drawer shows a
  labelled skeleton saying it is a 1-2 minute call, never a spinner that implies
  otherwise.

## What phase 2 adds

- **Real calendar, read-only**, through the approved Microsoft 365 connector
- **Prep briefs** that actually call the connector, streaming into the chat drawer
- **A polling scheduler**: 5 min for calendar in work hours, 30 min outside,
  paused in quiet hours, exponential backoff on failure, resync after sleep/wake
- **`meeting_in_5`** off a local timer — the only thing allowed to interrupt you
- **Conflict detection** computed locally, so it is right even offline
- **Honest status**: a polled source is never called "live"; the age is always stated

## Known limits

- **Slack chat is still scripted.** Mail drafts and calendar prep briefs are live;
  Slack is phase 4, and the drawer says so rather than passing a canned answer off
  as a reasoned one.
- **Automated senders are caught by a pattern list.** The real inbox is mostly
  `approvals@`, `health@aws`, `service-now` and similar, and each new one needs a
  pattern or a mute. Phase 5 sends genuinely ambiguous senders to Claude.
- **Threads group by subject.** The connector returns an empty conversation id for
  every message, so HELM strips `Re:`/`FW:` and groups on what is left — the way mail
  clients did before conversation ids existed. Two unrelated threads that share a
  subject will merge.
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
