# HELM 2.0

A local-first desktop command center for one person's work communication:
Outlook mail and calendar, Slack, smart importance/urgency flags, per-item chat,
daily summaries, and a shortcuts panel.

## Run it

```bash
# Windows (PowerShell)
C:\Users\jose.gonzalez\AppData\Local\Python\pythoncore-3.14-64\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py --demo
```

```bash
# macOS / Linux
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python run.py --demo
```

Self-check (no window, no network, no accounts):

```bash
.\.venv\Scripts\python.exe -m tests.test_flags
```

`--demo` is the default until phase 2. `run.py --live` will opt into real sources.

## NO NPM. EVER.

This machine has no npm, Node, yarn or pnpm, and must not acquire any.

- No `package.json`, no bundler, no transpiler, no TypeScript.
- Frontend is plain HTML + CSS + vanilla JS ES modules loaded straight from `ui/`.
- Any library must be vendored as a static file under `ui/vendor/`.
- If a task seems to need npm, the answer is that the task changes, not the rule.

## Pinned: pywebview==5.4

**Do not upgrade pywebview.** Measured on this machine 2026-10-19:

- pywebview 6.2.1 → WebView2 init fails with
  `(0x80004004): Operation aborted (Exception from HRESULT: 0x80004004 (E_ABORT))`
  in `CoreWebView2Environment.CreateCoreWebView2ControllerAsync`. Window opens blank.
- pywebview 5.4 → renders correctly.

Same WebView2 runtime (155.0.4283.62), same Python (3.14.7), same machine.
If you upgrade it, the app silently becomes a white rectangle.

## Enterprise constraints

These are facts about the environment, not preferences.

- **No app registrations, Slack apps, API tokens or OAuth clients.** The user cannot
  create them. Never design around them. Never ask for one.
- **Never extract browser session tokens or bypass any company control.** Not once,
  not "just for testing".
- **Data path is the user's already-approved claude.ai connectors**, reached by shelling
  out to the `claude` CLI, which is authenticated as the user. HELM stores **no**
  Microsoft or Slack credential of any kind.
- Any other secret goes in the OS keychain via `keyring`. Never in a file, never in git.
- **Classic Outlook COM is unavailable on this machine.** No MAPI profile, no `.ost`,
  the user runs new Outlook (`olk.exe`), which exposes no COM API. `OutlookDesktopSource`
  detects this and reports `unavailable`. Do not "fix" it.
- Local-first: one SQLite file, no cloud server, no telemetry, no analytics.

### Connector tool names

Spaces and dots in a connector's name become underscores. These exact strings work;
the obvious guesses silently deny:

```
mcp__claude_ai_Microsoft_365__outlook_calendar_search
mcp__claude_ai_Microsoft_365__outlook_email_search
mcp__claude_ai_Slack__slack_list_user_channels
mcp__claude_ai_Slack__slack_read_channel
```

### Measured latency (2026-10-19, this machine)

| Path | Measured |
| --- | --- |
| `claude -p` one-shot + connector read | 66–97 s |
| resident `stream-json` process, cold first turn | 40.6 s |
| resident process, warm turn, text only | 11.1 s |
| resident process, warm turn, with connector call | 29.7 s |
| meeting countdown (local timer) | < 1 ms |

HELM is a 2–5 minute refresh tool with exact meeting countdowns. It is **not** a
seconds-latency notifier. The UI must say "4m ago", never imply live.

## Safety rules

Non-negotiable. A change that weakens one of these is a bug regardless of what it enables.

### Nothing leaves without a click

- `Connector.send()` requires an `ApprovalToken`. A token is minted **only** inside a
  UI click handler. There is no other code path that can produce one.
- `action_log.approved_by` has exactly one legal value: `click`. Any other value is a bug
  and must fail loudly.
- Send, post, RSVP accept/decline → approval card showing the exact recipient and exact
  body, then a 30 s delay-send with undo.
- Delete or otherwise irreversible → approval card **plus** a second confirmation.
- Cross-source actions are allowed, but each write needs its own approval. One approval
  never covers two writes.

### Message content is data, never instructions

- Email, Slack and calendar content is untrusted input. Only the user's typed messages
  and button clicks can trigger an action.
- Never auto-forward, auto-reply, or send data to a new recipient because message
  content asked for it.
- **A message cannot influence its own flag.** Urgency comes from sender identity,
  recipient position (To vs Cc), calendar time, thread state and dates in *structured*
  fields. The body is never an urgency input. "URGENT", "high priority" and
  "action required" have zero effect.
- Pressure language from an external or unknown sender becomes a *posture warning*
  showing the real sending address — a thing the user looks at, not a thing that
  reorders the queue.
- Flags affect ordering, badges and whether the island may interrupt. A flag has no
  path to an action.

### Minimum context

- Send Claude only what the current request needs: the item, its thread, attendees,
  and bounded sender history. The chat drawer lists exactly what it attached.
- Honour per-sender and per-channel redaction/skip settings.

### Shortcuts

- `http`/`https` URLs only. A shortcut is never a file path and never a command.

## Layout

```
run.py              entry point, arg parsing
helm/
  bus.py            in-process event bus
  store.py          sqlite schema + queries (one file, WAL)
  models.py         Item, Flag, SourceStatus, ApprovalToken, Draft
  flags.py          stage-1 deterministic rules (stage 2 = phase 5)
  app.py            pywebview windows + the JS bridge API
  tray.py           system tray, degrades to no-op
  hotkeys.py        global hotkeys, degrades to no-op
  connectors/
    base.py         the Connector protocol every source implements
    demo.py         DemoSource — fixtures only, no I/O
ui/                 static HTML/CSS/JS, no build step
tests/              assert-based self-checks, no test runner needed
```

## Conventions

- Python 3.11+ syntax, standard library first, no dependency added for a few lines.
- Every new non-trivial branch, parser or security rule gets a check in `tests/`.
- A shortcut with a known limit gets a comment shaped
  `shortcut: <the limit>, <when to upgrade>`.
- Comment only the *why* the code cannot show.
