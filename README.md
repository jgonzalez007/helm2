# HELM 2.0

A local-first desktop command center for one person's work day: Outlook mail and
calendar, Slack, two-axis importance/urgency flags, per-item chat, daily summaries,
and a shortcuts panel. One Python codebase, Windows and macOS, **no npm**.

Phase 1 of 11 is complete: the whole UI running on fixtures.

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

## Check it

```bash
.\.venv\Scripts\python.exe -m tests.test_flags
```

```bash
.\.venv\Scripts\python.exe -m tests.test_api
```

```bash
.\.venv\Scripts\python.exe -m tests.smoke_window
```

`test_flags` (64 checks) covers the flag rules, including that a message cannot
influence its own flag. `test_api` (54 checks) covers the bridge, the approval gate
and the JS surface. `smoke_window` opens the real windows, drives the UI and reads
the rendered DOM back — it is the only one that needs a display.

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

## Known limits in phase 1

- **Chat replies are scripted.** No model is called. Real per-item chat is phase 3;
  measured warm round-trip will be about 11 s for text, 30 s with a connector call.
- **Search filters what is loaded.** SQLite FTS5 is phase 7.
- **Commitments are illustrative.** Extraction is phase 8.
- **Privacy mode is manual.** The panic hotkey and blur work; automatic
  screen-share and meeting-foreground detection is phase 6.
- **No real source is connected.** Calendar is phase 2, mail phase 3, Slack phase 4.
- **macOS is unverified.** Written from the docs, never run on a Mac.

## Why no npm, and why pywebview is pinned

See [CLAUDE.md](CLAUDE.md). Short version: this machine has no Node toolchain and must
not acquire one, and pywebview 6.2.1 fails WebView2 initialisation here with
`E_ABORT 0x80004004` while 5.4 works.
