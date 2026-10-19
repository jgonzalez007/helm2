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

Real calendar through the approved connectors:

```bash
.\.venv\Scripts\python.exe run.py --live
```

What the connectors can actually do right now:

```bash
.\.venv\Scripts\python.exe run.py --check
```

Self-checks (no window, no network, no accounts):

```bash
.\.venv\Scripts\python.exe -m tests.test_flags
```

```bash
.\.venv\Scripts\python.exe -m tests.test_api
```

```bash
.\.venv\Scripts\python.exe -m tests.test_phase2
```

Needs a display:

```bash
.\.venv\Scripts\python.exe -m tests.smoke_window
```

```bash
.\.venv\Scripts\python.exe -m tests.test_phase3
```

Needs the real connectors and reads your real data. Minutes, not seconds:

```bash
.\.venv\Scripts\python.exe -m tests.live_calendar --prep
```

```bash
.\.venv\Scripts\python.exe -m tests.live_mail --draft
```

`--demo` is the default. `--live` gives the real calendar and mail; Slack reports
`unavailable` until phase 4.

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
| calendar sweep, 25 events (phase 2, measured live) | 124–144 s |
| mail sweep, inbox + sent, two tool calls (phase 3) | 197–232 s |
| mail thread fetch, on open (phase 3) | 70 s |
| mail draft, cold session | 34 s |
| mail draft, warm session | 4.6 s |
| prep brief, cold session | 46 s |
| prep brief follow-up, warm session | 7.6 s |
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

### The tool allowlist is the real defence

Prompt wording is the weakest control in this app. The one that actually holds is
`--allowedTools`: every connector call names only the read-only tools that request
needs, and headless mode denies everything else. Even a fully hijacked agent cannot
send mail, post to Slack or RSVP, because those tools were never available to it.

- `claude_cli.WRITE_TOOLS` is never allowlisted by anything. It is passed as
  `--disallowedTools` on every call, so a future allowlist mistake still cannot
  reach it.
- Writes go through `Connector.send()` with a clicked `ApprovalToken`. That path is
  built in phase 10.
- A new allowlist constant must be covered by `tests/test_phase2.py`, which asserts
  no read allowlist intersects `WRITE_TOOLS`.

### Everything from a connector is hostile input

A calendar entry, a subject line or an attendee name may have been written by
anyone. `claude_source._clean_event` coerces every field to its expected type,
caps every string, bounds every list, and drops a join URL that is not `http(s)`.
Nothing is eval'd; no returned string is used as a path, command or recipient.

### Never show stale data as live

- A polled source is never labelled "live". `fmt.js freshness()` always states age.
- A failed sweep keeps the last good rows and flips the status dot. It must never
  empty a list — an inbox that looks calm because the connector broke is the one
  failure that makes the whole panel untrustworthy.
- Live mode never pads a missing source with fixtures. Unbuilt sources report
  `unavailable` and name their phase.

### Needs-reply is structural, like the flags

`mail_source.needs_reply()` asks who sent the newest message in the conversation,
whether the user was on To: rather than Cc:, and whether they have already replied.
It never reads the body. A question mark, "please confirm" or "ACTION REQUIRED"
changes nothing. The same rule as the flags, for the same reason, and
`tests/test_phase3.py` asserts identical structure gives an identical answer
regardless of body text.

### A thread costs a connector call

`poll()` only prefetches threads from sources with `prefetch_threads = True`.
Mail sets it `False` and fetches in `Api._fetch_thread` when the item is opened,
pushing `helm.onThreadLoaded`. Never add a per-item connector call to a sweep.

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
    _clean.py       field coercion shared by every real source
    claude_source.py  CalendarSource (phase 2)
    mail_source.py    MailSource (phase 3)
    pending.py      sources that are not built yet, and say so
ui/                 static HTML/CSS/JS, no build step
tests/              assert-based self-checks, no test runner needed
```

## Conventions

- Python 3.11+ syntax, standard library first, no dependency added for a few lines.
- Every new non-trivial branch, parser or security rule gets a check in `tests/`.
- A shortcut with a known limit gets a comment shaped
  `shortcut: <the limit>, <when to upgrade>`.
- Comment only the *why* the code cannot show.
