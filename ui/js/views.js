// View renderers. Each returns an HTML string for #main.
// They read from the shared state object and never call the bridge themselves.

import {
  badgePair, clockTime, countdown, esc, freshness, longDate, greeting, rel,
  IMPORTANCE_LABEL, URGENCY_LABEL, QUADRANT_LABEL,
} from './fmt.js';

export const NAV = [
  ['today', 'Today'],
  ['focus', 'Focus'],
  ['inbox', 'Inbox'],
  ['slack', 'Slack'],
  ['calendar', 'Calendar'],
  ['actions', 'Actions'],
  ['recap', 'Recap'],
  ['search', 'Search'],
  ['shortcuts', 'Shortcuts'],
];

const URGENT = new Set(['now', 'today']);

export function matchesFilter(flag, filter) {
  const imp = flag.importance === 'high';
  const urg = URGENT.has(flag.urgency);
  if (filter === 'important') return imp;
  if (filter === 'urgent') return urg;
  if (filter === 'both') return imp && urg;
  return true;
}

// Waiting items belong here too: the point of the tracker is that something you
// sent and nobody answered comes back to you, with a follow-up ready to draft.
export function focusRows(rows) {
  return rows.filter((r) => ['needs_me', 'waiting'].includes(r.item.state)
    && r.item.source !== 'calendar');
}

function row(r) {
  const { item, flag } = r;
  const who = item.sender || item.thread_id || '—';
  const age = item.received_at ? rel(item.received_at) : clockTime(item.starts_at);
  const warn = flag.posture_warning
    ? `<span class="row-warn" data-tip-warn="${esc(item.id)}" aria-label="Sender warning">⚠</span>`
    : '';
  return `<button class="row" data-item="${esc(item.id)}" data-state="${esc(item.state)}">
    ${badgePair(flag)}
    <span class="row-who">${esc(who)}</span>
    <span class="row-subject">${esc(item.subject)}
      <em>— ${esc(item.snippet.slice(0, 70))}</em></span>
    ${warn}
    <span class="row-age">${esc(age)}</span>
    <span class="row-chev" aria-hidden="true">›</span>
  </button>`;
}

function rowsOrEmpty(list, emptyText) {
  if (!list.length) return `<div class="empty">${esc(emptyText)}</div>`;
  return `<div class="rows">${list.map(row).join('')}</div>`;
}

function filterBar(filter) {
  const b = (key, label) => `<button data-filter="${key}"
    aria-pressed="${filter === key}">${label}</button>`;
  return `<span class="filters">${b('all', 'All')}${b('important', 'Important')}
    ${b('urgent', 'Urgent')}${b('both', 'Both')}</span>`;
}

function nextMeeting(rows) {
  return rows
    .filter((r) => r.item.source === 'calendar' && !r.item.all_day
      && Date.parse(r.item.starts_at) > Date.now())
    .sort((a, b) => Date.parse(a.item.starts_at) - Date.parse(b.item.starts_at))[0] || null;
}

// ── Today ─────────────────────────────────────────────────────────────────

export function viewToday(st) {
  const rows = st.rows.filter((r) => matchesFilter(r.flag, st.filter));
  const next = nextMeeting(st.rows);
  const needs = rows.filter((r) => r.item.state === 'needs_me' && r.item.source !== 'calendar');
  const waiting = rows.filter((r) => r.item.state === 'waiting');
  const later = rows.filter(
    (r) => r.item.state === 'needs_me' && r.flag.quadrant === 'neither',
  );

  const nextCard = next ? `<div class="nextup">
      <div class="nextup-top">
        <span class="nextup-time">${clockTime(next.item.starts_at)}</span>
        <span class="nextup-title">${esc(next.item.subject)}</span>
        <span class="nextup-cd" data-starts="${esc(next.item.starts_at)}">
          ${countdown(next.item.starts_at)}</span>
      </div>
      <div class="nextup-meta">${esc(next.item.attendees.join(', ') || 'no attendees listed')}
        · ${next.item.has_agenda ? 'agenda posted' : 'no agenda posted'}
        ${next.item.rsvp_pending ? ' · RSVP pending' : ''}
        ${next.item.conflicts_with.length ? ' · overlaps another meeting' : ''}</div>
      <div class="nextup-actions">
        <button class="btn btn--primary" data-join="${esc(next.item.join_url)}">Join</button>
        <button class="btn" data-prep="${esc(next.item.id)}">Prep brief</button>
        <button class="btn btn--ghost" data-item="${esc(next.item.id)}">Open</button>
      </div>
    </div>` : '<div class="empty">No meetings ahead today.</div>';

  return `
    <h2 class="sec">Next up</h2>
    ${nextCard}
    <h2 class="sec">Needs your reply <span class="count">${needs.length}</span>
      ${filterBar(st.filter)}</h2>
    ${rowsOrEmpty(needs, 'Nothing is waiting on you.')}
    <h2 class="sec">Waiting on others <span class="count">${waiting.length}</span></h2>
    ${rowsOrEmpty(waiting, 'You are not waiting on anyone.')}
    <h2 class="sec">Batched · low priority <span class="count">${later.length}</span></h2>
    ${rowsOrEmpty(later, 'Nothing batched.')}
  `;
}

// ── Focus ─────────────────────────────────────────────────────────────────

export function viewFocus(st) {
  const queue = focusRows(st.rows);
  if (!queue.length) {
    return '<h2 class="sec">Focus</h2><div class="empty">Queue is clear.</div>';
  }
  const i = Math.min(st.focusIndex, queue.length - 1);
  const { item, flag } = queue[i];
  const why = flag.reasons.map((r) => esc(r.detail)).join(' · ') || 'no rule matched';
  const isWaiting = item.state === 'waiting';

  return `
    <div class="focus-card">
      <div class="focus-head">
        <span class="focus-pos">Focus · ${i + 1} of ${queue.length}</span>
        ${badgePair(flag)}
        <span class="pill pill--waiting">${QUADRANT_LABEL[flag.quadrant]}</span>
        ${isWaiting ? `<span class="pill pill--working">waiting
          ${esc(rel(item.sent_by_me_at))}</span>` : ''}
      </div>
      <h3>${esc(item.subject)}</h3>
      <div class="focus-from">${isWaiting
    ? `you sent this ${esc(rel(item.sent_by_me_at))} ago · no reply yet`
    : `${esc(item.sender)} · ${esc(item.sender_addr)}
        · ${item.to_me_direct ? 'to: you' : 'cc only'} · ${rel(item.received_at)} ago`}</div>
      <div class="focus-body">${esc(item.snippet)}</div>
      ${flag.posture_warning
    ? `<div class="d-warn">⚠ ${esc(flag.posture_warning)}</div>` : ''}
      <div class="focus-why"><b>why flagged</b> — ${why}</div>
      <div class="focus-actions">
        ${isWaiting
    ? '<button class="btn btn--primary" data-focus-act="followup"><kbd>r</kbd> '
      + 'Draft follow-up</button>'
    : '<button class="btn btn--primary" data-focus-act="reply"><kbd>r</kbd> Reply</button>'}
        <button class="btn" data-focus-act="done"><kbd>e</kbd> Done</button>
        <button class="btn" data-focus-act="snooze"><kbd>s</kbd> Snooze</button>
        <button class="btn" data-focus-act="delegate"><kbd>d</kbd> Delegate</button>
        <span class="spacer"></span>
        <button class="btn btn--ghost" data-override="not_important"
          data-item-id="${esc(item.id)}">not important</button>
        <button class="btn btn--ghost" data-override="more_urgent"
          data-item-id="${esc(item.id)}">more urgent ↑</button>
      </div>
    </div>`;
}

// ── Source lists ──────────────────────────────────────────────────────────

export function viewSource(st, source, title) {
  const rows = st.rows
    .filter((r) => r.item.source === source && matchesFilter(r.flag, st.filter));
  const needs = rows.filter((r) => r.item.state === 'needs_me');
  const rest = rows.filter((r) => r.item.state !== 'needs_me');
  return `
    <h2 class="sec">${esc(title)} · needs you <span class="count">${needs.length}</span>
      ${filterBar(st.filter)}</h2>
    ${rowsOrEmpty(needs, 'Nothing here needs you.')}
    <h2 class="sec">Everything else <span class="count">${rest.length}</span></h2>
    ${rowsOrEmpty(rest, 'Empty.')}
  `;
}

export function viewCalendar(st) {
  const events = st.rows
    .filter((r) => r.item.source === 'calendar')
    .sort((a, b) => Date.parse(a.item.starts_at) - Date.parse(b.item.starts_at));
  const conflicts = events.filter((r) => r.item.conflicts_with.length);
  return `
    <h2 class="sec">Today's meetings <span class="count">${events.length}</span></h2>
    ${rowsOrEmpty(events, 'Clear calendar.')}
    ${conflicts.length ? `<h2 class="sec">Conflicts
      <span class="count">${conflicts.length}</span></h2>
      ${rowsOrEmpty(conflicts, '')}` : ''}
  `;
}

// ── Actions ───────────────────────────────────────────────────────────────

export function viewActions(st) {
  const log = st.log || [];
  if (!log.length) {
    return '<h2 class="sec">Action log</h2><div class="empty">No actions yet.</div>';
  }
  return `<h2 class="sec">Action log <span class="count">${log.length}</span></h2>
    <div class="card" style="max-width:none">
    <p>Everything HELM did, with the exact content. A send shows
      <b>approved by click</b> — there is no other way one can happen.</p>
    <table class="log">
      <thead><tr><th>When</th><th>What</th><th>Target</th><th>Status</th>
        <th>Approval</th><th>Content</th></tr></thead>
      <tbody>${log.map((a) => `<tr>
        <td class="when">${esc(a.at.replace('T', ' ').slice(0, 19))}</td>
        <td>${esc(a.verb)}</td>
        <td>${esc(a.target.slice(0, 48))}</td>
        <td>${esc(a.status)}</td>
        <td>${a.approved_by ? `<span class="pill pill--done">${esc(a.approved_by)}</span>`
    : '<span class="pill">—</span>'}</td>
        <td class="payload">${esc(a.payload.slice(0, 90))}</td>
      </tr>`).join('')}</tbody>
    </table></div>`;
}

// ── Recap ─────────────────────────────────────────────────────────────────

function byId(st, ids) {
  const map = new Map(st.rows.map((r) => [r.item.id, r]));
  return (ids || []).map((id) => map.get(id)).filter(Boolean);
}

function commitmentRow(c) {
  const due = c.due_at ? `due ${clockDay(c.due_at)}` : 'no date stated';
  return `<div class="field">
    <label>${esc(c.text)}<small>${esc(c.owed_to || 'recipient not recorded')}
      · ${esc(due)} · “${esc((c.source_quote || '').slice(0, 90))}”</small></label>
    ${c.state === 'open'
    ? `<button class="btn btn--ghost" data-commit-done="${c.id}">mark done</button>`
    : '<span class="pill pill--done">done</span>'}
  </div>`;
}

function clockDay(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
}

function metric(label, value, note) {
  return `<div class="field"><label>${esc(label)}${note
    ? `<small>${esc(note)}</small>` : ''}</label>
    <span class="pill">${value === null || value === undefined ? '—' : esc(String(value))}</span>
  </div>`;
}

export function viewRecap(st) {
  const s = st.summaries || {};
  const brief = s.brief || {};
  const recap = s.recap || {};
  const week = s.weekly || {};
  const commits = s.commitments || [];
  const slot = brief.time_block?.slot || {};
  const open = commits.filter((c) => c.state === 'open');

  return `
    <h2 class="sec">Morning brief
      <span class="count">${(brief.meetings || []).length} meetings</span></h2>
    <div class="card">
      <h3>Today's meetings</h3>
      ${(brief.meetings || []).length
    ? (brief.meetings).map((m) => `<div class="field">
        <label>${clockTime(m.starts_at)} · ${esc(m.subject)}
          <small>${esc(m.attendees.join(', ') || 'no attendees listed')}${m.gaps.length
    ? ` · ${esc(m.gaps.join(' · '))}` : ''}</small></label>
        <button class="btn btn--ghost" data-prep="${esc(m.id)}">Prep</button>
      </div>`).join('')
    : '<div class="empty">Nothing in the calendar today.</div>'}
    </div>

    <div class="card">
      <h3>Needs a reply <span class="pill pill--waiting">${
  (brief.needs_reply || []).length}</span></h3>
      ${rowsOrEmpty(byId(st, brief.needs_reply), 'Nothing urgent is waiting on you.')}
    </div>

    <div class="card">
      <h3>Waiting on others <span class="pill pill--working">${
  (brief.waiting_on || []).length}</span></h3>
      ${rowsOrEmpty(byId(st, brief.waiting_on), 'You are not waiting on anyone.')}
    </div>

    <div class="card">
      <h3>Important, not urgent</h3>
      <p>These never shout, so they get a time block instead of a badge.</p>
      ${rowsOrEmpty(byId(st, brief.time_block?.items), 'Nothing in this quadrant.')}
      <div class="field"><label>Suggested block<small>${slot.found
    ? `${clockDay(slot.start)}, ${clockTime(slot.start)}–${clockTime(slot.end)},
       the first clear hour in your calendar`
    : esc(slot.reason || 'no calendar data to find a gap in')}</small></label></div>
    </div>

    <h2 class="sec">End of day</h2>
    <div class="card">
      <h3>Handled <span class="pill pill--done">${(recap.handled || []).length}</span></h3>
      ${rowsOrEmpty(byId(st, recap.handled), 'Nothing cleared yet today.')}
    </div>
    <div class="card">
      <h3>Still open <span class="pill pill--waiting">${
  (recap.still_open || []).length}</span></h3>
      ${rowsOrEmpty(byId(st, (recap.still_open || []).slice(0, 6)), 'Clear.')}
    </div>
    <div class="card">
      <h3>Tomorrow's first meetings</h3>
      ${(recap.tomorrow || []).length
    ? (recap.tomorrow).map((m) => `<div class="field"><label>${clockTime(m.starts_at)}
        · ${esc(m.subject)}</label></div>`).join('')
    : '<div class="empty">Nothing scheduled tomorrow.</div>'}
    </div>

    <h2 class="sec">Commitments
      <span class="count">${open.length} open</span></h2>
    <div class="card">
      <h3>What you promised</h3>
      <p>Found in your own sent messages by plain string matching — no model call,
        and nothing anyone else wrote is ever read. Only first-person promises count,
        so “can you send X” is not one.</p>
      ${commits.length ? commits.map(commitmentRow).join('')
    : '<div class="empty">No promises found in what you have sent.</div>'}
      <div class="nextup-actions" style="margin-top:12px">
        <button class="btn" id="refresh-commitments">Look again</button>
      </div>
    </div>

    <h2 class="sec">This week</h2>
    <div class="card">
      <h3>Metrics</h3>
      <p>Counted, not estimated. Anything HELM cannot measure honestly shows a dash.</p>
      ${metric('Items cleared', week.items_cleared)}
      ${metric('Median response time', week.median_response_hours === null
    || week.median_response_hours === undefined
    ? null : `${week.median_response_hours} h`,
  `across ${week.replies_counted || 0} replies this week`)}
      ${metric('Meetings', week.meetings_total,
    week.busiest_day ? `busiest day ${clockDay(week.busiest_day)}` : '')}
      ${metric('Meetings per day', week.meetings_per_day, 'on days you had any')}
      ${metric('Flag accuracy', week.flag_accuracy === null
    || week.flag_accuracy === undefined ? null : `${week.flag_accuracy}%`,
  `${week.flags_corrected || 0} of ${week.flags_total || 0} flags needed a correction`)}
      ${metric('Actions logged', week.actions_logged)}
    </div>`;
}

// ── Search ────────────────────────────────────────────────────────────────

const FIELD_LABEL = { item: 'subject', thread: 'in the thread', chat: 'in your chat' };

/** A hit row: the item, plus where the match was and the words around it. */
function hitRow(r) {
  const { item, flag, excerpt, field } = r;
  const who = item.sender || item.thread_id || '—';
  const age = item.received_at ? rel(item.received_at) : clockTime(item.starts_at);
  return `<button class="row row--hit" data-item="${esc(item.id)}"
      data-state="${esc(item.state)}">
    ${badgePair(flag)}
    <span class="row-who">${esc(who)}</span>
    <span class="row-subject">${esc(item.subject)}
      <em class="hit-excerpt">${mark(excerpt)}</em></span>
    <span class="hit-where">${esc(FIELD_LABEL[field] || field)}</span>
    <span class="row-age">${esc(age)}</span>
  </button>`;
}

/** The index wraps matches in ‹…›; everything else is escaped before unwrapping. */
function mark(excerpt) {
  return esc(excerpt || '')
    .split('‹').join('<mark>')
    .split('›').join('</mark>');
}

export function viewSearch(st) {
  const q = (st.query || '').trim();
  const s = st.search || {};
  const hits = (st.results || []).filter((r) => matchesFilter(r.flag, st.filter));

  const note = s.available === false
    ? 'This SQLite build has no FTS5, so search is unavailable. Nothing else is affected.'
    : `Mail, Slack, meetings and item chats. ${s.indexed || 0} entries indexed `
      + 'locally — instant, offline, no connector call.';

  return `
    <h2 class="sec">Search ${filterBar(st.filter)}</h2>
    <div class="card" style="max-width:none">
      <div class="field">
        <label for="q">Across everything HELM has seen<small>${esc(note)}</small></label>
        <input id="q" type="search" value="${esc(st.query || '')}"
          placeholder="type to search" autocomplete="off" spellcheck="false">
      </div>
    </div>
    ${!q ? '<div class="empty">Type to search.</div>'
    : hits.length ? `<div class="rows">${hits.map(hitRow).join('')}</div>`
      : `<div class="empty">No matches for “${esc(q)}”.</div>`}`;
}

// ── Shortcuts ─────────────────────────────────────────────────────────────

export function viewShortcuts(st) {
  const groups = {};
  (st.shortcuts || []).forEach((s, i) => {
    (groups[s.group_name] ||= []).push({ ...s, index: i });
  });

  const body = Object.entries(groups).map(([name, list]) => `
    <div class="card">
      <h3>${esc(name)}</h3>
      ${list.map((s) => `<div class="sc-row">
        <kbd>${s.index < 9 ? `Alt+${s.index + 1}` : ''}</kbd>
        <span class="lbl">${esc(s.label)}</span>
        <span class="url">${esc(s.url)}</span>
        <button class="btn btn--ghost" data-open-url="${esc(s.url)}">Open</button>
        <button class="btn btn--ghost btn--danger" data-del-sc="${s.id}">✕</button>
      </div>`).join('')}
    </div>`).join('');

  return `
    <h2 class="sec">Shortcuts</h2>
    ${body || '<div class="empty">No shortcuts yet.</div>'}
    <div class="card">
      <h3>Add a link</h3>
      <p>http and https only. A shortcut is never a file and never a command.</p>
      <div class="field">
        <label for="sc-label">Label</label>
        <input id="sc-label" type="text" placeholder="Jira">
      </div>
      <div class="field">
        <label for="sc-url">URL</label>
        <input id="sc-url" type="text" placeholder="https://…">
      </div>
      <div class="field">
        <label for="sc-group">Group</label>
        <input id="sc-group" type="text" placeholder="Work">
      </div>
      <div class="nextup-actions">
        <button class="btn btn--primary" id="sc-add">Add</button>
        <button class="btn" id="sc-export">Export JSON</button>
        <button class="btn" id="sc-import">Import JSON</button>
      </div>
      <div id="sc-msg" style="margin-top:10px;font-size:12.5px;color:var(--mute)"></div>
    </div>`;
}

// ── Settings ──────────────────────────────────────────────────────────────

/** Demo mode never probes the connectors, so it reports "not checked" rather than
 *  "not connected" — claiming a failure HELM did not observe is still a lie. */
function connectorRow(label, sched, present, note, yes, no) {
  const state = sched.demo ? 'demo' : (present ? 'live' : 'unavailable');
  const text = sched.demo ? 'not checked in demo mode' : (present ? yes : no);
  return `<div class="field">
    <label>${esc(label)}<small>${esc(note)}</small></label>
    <span class="status"><span class="dot dot--${state}"></span>${esc(text)}</span>
  </div>`;
}

export function viewSettings(st) {
  const s = st.settings || {};
  const sched = st.schedule || {};
  const sel = (key, options, value) => `<select data-set="${key}">${
    options.map(([v, l]) => `<option value="${v}" ${v === value ? 'selected' : ''}>${l}</option>`)
      .join('')}</select>`;
  const check = (key, value) => `<input type="checkbox" data-set="${key}"
    ${value ? 'checked' : ''}>`;
  const list = (key, value) => `<input type="text" data-set-list="${key}"
    value="${esc((value || []).join(', '))}" placeholder="comma separated">`;

  return `
    <h2 class="sec">Settings</h2>

    <div class="card">
      <h3>Accounts</h3>
      <p>HELM stores no Microsoft or Slack credential. It shells out to the
        <code>claude</code> CLI, which is already authenticated as you through the
        connectors your organization approved.</p>
      ${connectorRow('claude CLI', sched, sched.availability?.cli,
    'the only thing that touches your mail and Slack', 'found', 'not found')}
      ${connectorRow('Microsoft 365 connector', sched, sched.availability?.microsoft_365,
    'calendar now, mail from phase 3', 'connected', 'not connected')}
      ${connectorRow('Slack connector', sched, sched.availability?.slack,
    'used from phase 4', 'connected', 'not connected')}
      ${sched.availability?.error
    ? `<div class="field"><label>Last error<small>${esc(sched.availability.error)}</small>
        </label></div>` : ''}
    </div>

    <div class="card">
      <h3>Sources and refresh cadence</h3>
      <p>${sched.demo
    ? 'Demo mode reads fixtures, so nothing is polled and nothing is fetched.'
    : `A sweep is a model turn with a real token cost, so the cadence is the main
       cost control. Currently ${sched.work_time ? 'work hours' : 'off hours'}.`}
        ${sched.paused ? `<b>Paused: ${esc(sched.paused)}.</b>` : ''}</p>
      ${(st.sources || []).map((src) => {
    const row = sched.sources?.[src.source] || {};
    const cadence = row.interval_s ? `every ${Math.round(row.interval_s / 60)} min` : '—';
    const next = row.next_in_s != null ? `, next in ${row.next_in_s}s` : '';
    const back = row.backoff_s ? `, backing off ${row.backoff_s}s` : '';
    return `<div class="field">
        <label>${esc(src.source)}<small>${esc(src.last_error
      || `${cadence}${next}${back} · last round-trip ${src.measured_latency_ms} ms`)}</small>
        </label>
        <span class="status"><span class="dot dot--${esc(src.health)}"></span>
          ${esc(freshness(src))}</span>
      </div>`;
  }).join('')}
      <div class="nextup-actions" style="margin-top:12px">
        <button class="btn" id="resync">Refresh now</button>
      </div>
    </div>

    <div class="card">
      <h3>Appearance</h3>
      <div class="field"><label for="theme">Theme</label>
        ${sel('theme', [['system', 'System'], ['light', 'Light'], ['dark', 'Dark']], s.theme)}</div>
      <div class="field"><label>Density</label>
        ${sel('density', [['comfortable', 'Comfortable'], ['compact', 'Compact']], s.density)}</div>
      <div class="field"><label>Professional mode
        <small>mute the character for meetings and shared screens</small></label>
        ${check('professional_mode', s.professional_mode)}</div>
      <div class="field"><label>Reduced motion</label>
        ${check('reduced_motion', s.reduced_motion)}</div>
      <div class="field"><label>Sounds<small>off by default</small></label>
        ${check('sounds', s.sounds)}</div>
    </div>

    ${st.realtime ? `<div class="card">
      <h3>Realtime paths</h3>
      <p>Every other mail path here is a 2–5 minute poll. These two are the only
        ways to get seconds, and both say plainly whether they work on this machine.</p>
      <div class="field">
        <label>Drop folder<small>${esc(st.realtime.dropfolder.folder
    || 'not configured')}</small></label>
        <span class="pill ${st.realtime.dropfolder.status?.health === 'live'
    ? 'pill--done' : 'pill--alert'}">${esc(
  st.realtime.dropfolder.status?.health || 'unknown')}</span>
      </div>
      ${st.realtime.dropfolder.status?.last_error
    ? `<div class="field"><label>Why<small>${esc(
      st.realtime.dropfolder.status.last_error)}</small></label></div>` : `
      <div class="field"><label>Files read<small>${
  st.realtime.dropfolder.rejected} rejected and kept for inspection</small></label>
        <span class="pill">${st.realtime.dropfolder.seen}</span></div>`}
      <div class="field">
        <label>Classic Outlook<small>${esc(
    st.realtime.outlook_desktop.detection?.detail
      || st.realtime.outlook_desktop.detection?.reason || '')}</small></label>
        <span class="pill ${st.realtime.outlook_desktop.status?.health === 'live'
    ? 'pill--done' : 'pill--alert'}">${esc(
  st.realtime.outlook_desktop.status?.health || 'unknown')}</span>
      </div>
      <div class="field"><label>What would change it<small>${esc(
    st.realtime.outlook_desktop.detection?.remedy || '')}</small></label></div>
    </div>` : ''}

    <div class="card">
      <h3>Privacy</h3>
      <p>The panel hides itself when something is on screen with you. What this can
        honestly detect is the app in front — not whether you are sharing. Teams
        gives no public signal for that, so HELM does not claim it.</p>
      <div class="field">
        <label>Right now<small>${esc(st.privacy?.watching
    ? 'watching the foreground window, locally, every 1.5 s'
    : 'automatic detection is Windows-only; the panic hotkey still works')}</small></label>
        <span class="pill ${st.privacy?.on ? 'pill--alert' : 'pill--done'}">${
  st.privacy?.on ? 'hidden' : 'visible'}</span>
      </div>
      ${st.privacy?.reason ? `<div class="field"><label>Reason
        <small>${esc(st.privacy.reason)}${st.privacy.app
    ? ` · ${esc(st.privacy.app)}` : ''}</small></label></div>` : ''}
      <div class="field"><label>Hide automatically
        <small>when a meeting app is in front, or a Zoom share is running</small></label>
        ${check('privacy_auto', s.privacy_auto !== false)}</div>
      <div class="field"><label>Apps that trigger it
        <small>executable names, matched exactly</small></label>
        ${list('privacy_apps', s.privacy_apps)}</div>
      <div class="nextup-actions" style="margin-top:12px">
        <button class="btn" id="privacy-toggle">${st.privacy?.on
    ? 'Show the panel' : 'Hide the panel now'}</button>
        <span style="align-self:center;color:var(--mute);font-size:12.5px">
          or Ctrl+Shift+\\ from anywhere</span>
      </div>
    </div>

    <div class="card">
      <h3>Search index</h3>
      <p>A local SQLite index over everything HELM has seen: subjects, previews,
        thread messages and your own item chats. No connector call, no model call,
        and it works offline.</p>
      <div class="field">
        <label>Entries indexed<small>${st.search?.available === false
    ? 'this SQLite build has no FTS5, so search is unavailable'
    : 'rebuilt automatically when the index is empty'}</small></label>
        <span class="pill ${st.search?.available === false
    ? 'pill--alert' : 'pill--done'}">${st.search?.indexed ?? 0}</span>
      </div>
      <div class="nextup-actions" style="margin-top:12px">
        <button class="btn" id="reindex">Rebuild the index</button>
      </div>
    </div>

    <div class="card">
      <h3>Your writing voice</h3>
      <p>Learned from your own sent mail, with plain string matching rather than a
        model call. Drafts are written to match it.</p>
      <div class="field">
        <label>Current profile<small>${esc(st.voice || 'not built yet')}</small></label>
      </div>
      <div class="nextup-actions" style="margin-top:12px">
        <button class="btn" id="rebuild-voice">Relearn from my sent mail</button>
      </div>
      <div id="voice-msg" style="margin-top:10px;font-size:12.5px;color:var(--mute)"></div>
    </div>

    <div class="card">
      <h3>What HELM has learned about senders</h3>
      <p>A click teaches it for good; the rest it worked out once and cached, so the
        same sender is never judged twice. Pinned rows came from you and no
        classifier overwrites them.</p>
      ${(st.senders || []).length
    ? (st.senders || []).map((r) => `<div class="sc-row">
        <kbd>${r.pinned ? 'yours' : (r.decided_by === 'claude'
      ? (r.confidence != null ? r.confidence.toFixed(2) : 'claude') : 'rules')}</kbd>
        <span class="lbl">${r.is_automated ? 'automated' : 'person'}</span>
        <span class="url">${esc(r.addr)} — ${esc(r.reason || 'no reason recorded')}</span>
        <button class="btn btn--ghost" data-forget="${esc(r.addr)}">Forget</button>
      </div>`).join('')
    : '<div class="empty">Nothing learned yet.</div>'}
      <div class="nextup-actions" style="margin-top:12px">
        <button class="btn" id="classify-now">Judge the unclear ones now</button>
      </div>
      <div id="classify-msg" style="margin-top:10px;font-size:12.5px;color:var(--mute)"></div>
    </div>

    <div class="card">
      <h3>People and noise</h3>
      <p>Stage-1 flags are only as good as these lists. Editing one reflags everything
        except items you have pinned with an override.</p>
      <div class="field"><label>Managers<small>importance → high</small></label>
        ${list('managers', s.managers)}</div>
      <div class="field"><label>VIPs<small>importance → high</small></label>
        ${list('vips', s.vips)}</div>
      <div class="field"><label>Muted senders<small>importance → low</small></label>
        ${list('muted_senders', s.muted_senders)}</div>
      <div class="field"><label>Muted channels</label>
        ${list('muted_channels', s.muted_channels)}</div>
      <div class="field"><label>Internal domains
        <small>anything else counts as external</small></label>
        ${list('internal_domains', s.internal_domains)}</div>
    </div>

    <div class="card">
      <h3>Quiet hours and summaries</h3>
      <div class="field"><label>Quiet from</label>
        <input type="time" data-set-time="quiet_hours.start"
          value="${esc(s.quiet_hours?.start || '19:00')}"></div>
      <div class="field"><label>Quiet until</label>
        <input type="time" data-set-time="quiet_hours.end"
          value="${esc(s.quiet_hours?.end || '07:00')}"></div>
      <div class="field"><label>Morning brief</label>
        <input type="time" data-set-time="summary_times.brief"
          value="${esc(s.summary_times?.brief || '08:00')}"></div>
      <div class="field"><label>End-of-day recap</label>
        <input type="time" data-set-time="summary_times.recap"
          value="${esc(s.summary_times?.recap || '17:30')}"></div>
    </div>

    <div class="card">
      <h3>Demo</h3>
      <p>Reset the fixtures and the flags learned from them. Your settings and
        shortcuts are kept.</p>
      <button class="btn" id="demo-reset">Reset demo data</button>
      <button class="btn btn--ghost" id="show-firstrun">Replay the first-run tour</button>
    </div>`;
}

// The header shows the three sources you read. The realtime paths live in
// Settings, where their status comes with the reason and the remedy.
const HEADER_SOURCES = ['mail', 'slack', 'calendar'];

export function header(st) {
  const g = `${greeting()}, Jose <span>· ${longDate()}</span>`;
  const statuses = (st.sources || [])
    .filter((src) => HEADER_SOURCES.includes(src.source))
    .map((src) => `<span class="status"
    data-tip-source="${esc(src.source)}">
    <span class="dot dot--${esc(src.health)}"></span>${esc(src.source)} ${esc(freshness(src))}
  </span>`).join('');
  return { greeting: g, statuses };
}

export function navHtml(st) {
  const counts = {};
  for (const r of st.rows) {
    if (r.item.state !== 'needs_me') continue;
    if (r.item.source === 'mail') counts.inbox = (counts.inbox || 0) + 1;
    if (r.item.source === 'slack') counts.slack = (counts.slack || 0) + 1;
  }
  counts.focus = focusRows(st.rows).length;
  const hot = focusRows(st.rows)
    .filter((r) => r.flag.quadrant === 'important_urgent').length;

  return NAV.map(([key, label]) => {
    const n = counts[key];
    const isHot = key === 'focus' && hot > 0;
    return `<button class="nav-item" data-view="${key}"
      ${st.view === key ? 'aria-current="page"' : ''}>
      ${label}${n ? `<span class="n" data-hot="${isHot ? 1 : 0}">${n}</span>` : ''}
    </button>`;
  }).join('');
}

export function tooltipHtml(flag) {
  const src = flag.decided_by === 'claude'
    ? `Claude · confidence ${(flag.confidence ?? 0).toFixed(2)}`
    : 'rules only ✓';
  const reasons = flag.reasons.length
    ? `<ul>${flag.reasons.map((r) => `<li>${esc(r.detail)}</li>`).join('')}</ul>`
    : '<ul><li>no rule matched; left at the default</li></ul>';
  const pinned = flag.pinned
    ? '<div class="tt-foot">Pinned by your override. Never reclassified.</div>'
    : '<div class="tt-foot">Message text was not read for urgency cues.</div>';
  return `<h5>${IMPORTANCE_LABEL[flag.importance]} · ${URGENCY_LABEL[flag.urgency]}
    <span>${src}</span></h5>${reasons}${pinned}`;
}
