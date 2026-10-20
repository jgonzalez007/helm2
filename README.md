# HELM 2.0

A local-first desktop command center for one person's work day: Outlook mail and
calendar, Slack, two-axis importance/urgency flags, per-item chat, daily summaries,
and a shortcuts panel. One Python codebase, Windows and macOS, **no npm**.

Phases 1 to 6 of 11 are complete: the whole UI on fixtures, a real read-only
calendar, real mail and real Slack — triage, needs-reply detection and drafts in your
own voice — through the connectors your organization already approved, plus the focus
queue, automatic privacy mode and the waiting-on tracker.

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

Your real calendar, mail and Slack — read-only plus drafts. Nothing can be sent or
posted: that is phase 10.

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
.\.venv\Scripts\python.exe -m tests.test_phase3
```

```bash
.\.venv\Scripts\python.exe -m tests.test_phase4
```

```bash
.\.venv\Scripts\python.exe -m tests.test_phase5
```

```bash
.\.venv\Scripts\python.exe -m tests.test_phase6
```

Needs a display:

```bash
.\.venv\Scripts\python.exe -m tests.smoke_window
```

Needs the connectors and reads your own data. Minutes, not seconds:

```bash
.\.venv\Scripts\python.exe -m tests.live_calendar --prep
```

```bash
.\.venv\Scripts\python.exe -m tests.live_mail --draft
```

```bash
.\.venv\Scripts\python.exe -m tests.live_slack --draft
```

```bash
.\.venv\Scripts\python.exe -m tests.live_classify
```

`test_flags` (64 checks) covers the flag rules, including that a message cannot
influence its own flag. `test_api` (54 checks) covers the bridge, the approval gate
and the JS surface. `test_phase2` (134 checks) covers connector parsing, hostile
input, polling cadence and the tool allowlists. `smoke_window` opens the real
windows, drives the UI and reads the rendered DOM back — it needs a display.
`test_phase3` (154 checks) covers mail parsing, needs-reply, waiting-on and the voice
profile. `test_phase4` (115 checks) covers Slack parsing, broadcasts versus mentions and Slack drafts. `test_phase5` (72 checks) covers stage-2 classification, its caching, and
the two limits above. `test_phase6` (60 checks) covers the privacy rules, snooze, and that a sweep cannot
undo a click. `live_calendar`, `live_mail` and `live_classify` each do one real pass
against your own data.

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

## What phase 6 adds

- **Automatic privacy mode.** The panel hides itself when a meeting app is in the
  foreground, locally, from one window handle every 1.5 s. No connector call, no cost.
- **It does not claim what it cannot see.** Teams gives no public signal for "you are
  sharing right now", so HELM says "a meeting app is in the foreground" instead.
  Zoom's `cpthost.exe` is a real share signal and is named as one.
- **A window title can never trigger it.** A title is written by whoever sent the
  invite; only the executable name counts.
- **Nothing interrupts over a shared screen, a fullscreen app, or during quiet
  hours.** Every refusal names its reason in the console rather than vanishing.
- **A snooze ends.** It lands at 08:00 local and a local timer puts the item back.
  A fixed offset returned things at whatever hour you happened to click.
- **Waiting-on reaches the focus queue** with `r` bound to "draft a follow-up" rather
  than "reply" — nobody has replied yet, so there is nothing to answer.
- **A sweep can no longer undo a click.** This was a real bug: every poll rebuilt each
  item from scratch and overwrote `state`, silently reopening anything marked Done or
  Snoozed. The store now treats `state`, `read_at`, `replied_at` and `snooze_until`
  as yours, not the source's.

## What phase 4 adds

- **Real Slack**: one sweep, three tool calls — who you are, your DMs and group DMs,
  and messages that mention you
- **`<!here>` is not a mention of you.** The only "mention" in a real three-day
  search was a `<!here> Team,` broadcast. A direct ping requires your own user id in
  the message, so channel announcements never reach the focus queue.
- **Needs-reply from who spoke last**: your own messages are identified by the
  connector's `is_me`, not by comparing display names
- **One VIP list covers both sources.** Slack carries no email addresses, so a handle
  plus your primary internal domain is used as the address — every handle observed
  here is exactly the email local part.
- **Slack markup flattened** for list rows: `<@U123>`, `<#C1|chan>`, `<https://x|y>`
- **Drafts** addressed to the conversation, shorter and plainer than email, every one
  ending in an approval card marked `post` rather than `send`
- **A group conversation between other people is not your reply.** In a room with
  several participants you must be part of the exchange, and a conversation nobody has
  touched in 14 days drops out of the queue. On the real workspace this cut the queue
  from four items to one, each exclusion naming its reason.

## What phase 5 adds

- **Stage 2**: Claude judges the items the rules could not settle, batched, with a
  confidence shown in the tooltip
- **Importance only.** Stage 2 can never set urgency — that stays with the calendar,
  parsed deadlines and thread state. Enforced in code, not by prompt wording.
- **No body is sent.** The payload is built from a named allowlist of metadata
  fields, and stage 2 is given no tools at all.
- **A sender is judged once.** The verdict is remembered, so the hundredth message
  from `health@aws.com` costs nothing. This replaces the hardcoded pattern list.
- **A click outranks any model**, permanently. "Not important" on a sender teaches
  HELM for good, and no classifier can undo it. Settings lists everything it has
  learned, with a Forget button on each row.

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

- **Chat beyond drafting is still scripted.** Mail and Slack drafts and calendar prep
  briefs are live; the other quick actions say so rather than passing a canned answer
  off as a reasoned one.
- **Slack handles are assumed to match email local parts.** True for every handle
  observed in this workspace, and Settings shows the derived address, but a
  workspace where they differ would not match your VIP list.
- **Slack mentions come from a 3-day search.** Older mentions are not swept.
- **Slack sweeps read 6 conversations deep.** A busier workspace would need paging.
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
- **Privacy detection is Windows-only.** The panic hotkey and the manual toggle work
  everywhere; the foreground watcher needs pywin32. Settings says which you have.
- **macOS is unverified.** Written from the docs, never run on a Mac.

## Why no npm, and why pywebview is pinned

See [CLAUDE.md](CLAUDE.md). Short version: this machine has no Node toolchain and must
not acquire one, and pywebview 6.2.1 fails WebView2 initialisation here with
`E_ABORT 0x80004004` while 5.4 works.
