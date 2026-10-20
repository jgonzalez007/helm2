// Panel controller: routing, keyboard, the item drawer and the approval flow.

import { call, on, ready } from './bridge.js';
import { countdown, esc, QUADRANT_LABEL } from './fmt.js';
import * as V from './views.js';

const UNDO_SECONDS = 30;

const st = {
  view: 'today',
  filter: 'all',
  focusIndex: 0,
  query: '',
  rows: [],
  sources: [],
  shortcuts: [],
  settings: {},
  log: [],
  hotkeys: {},
  schedule: {},
  voice: '',
  senders: [],
  privacy: {},
  search: {},
  summaries: {},
  realtime: null,
  results: [],
  queryToken: 0,
  openItem: null,
  detail: null,
};

const el = (id) => document.getElementById(id);

// ── Render ────────────────────────────────────────────────────────────────

const RENDER = {
  today: V.viewToday,
  focus: V.viewFocus,
  inbox: (s) => V.viewSource(s, 'mail', 'Inbox'),
  slack: (s) => V.viewSource(s, 'slack', 'Slack'),
  calendar: V.viewCalendar,
  actions: V.viewActions,
  recap: V.viewRecap,
  search: V.viewSearch,
  shortcuts: V.viewShortcuts,
  settings: V.viewSettings,
};

function render() {
  const h = V.header(st);
  el('greeting').innerHTML = h.greeting;
  el('statuses').innerHTML = h.statuses;
  el('nav').innerHTML = V.navHtml(st);
  el('main').innerHTML = (RENDER[st.view] || V.viewToday)(st);
  el('shortcutbar').innerHTML = st.shortcuts.map((s, i) => `<button class="sc"
    data-open-url="${esc(s.url)}">${esc(s.label)}
    ${i < 9 ? `<kbd>⌥${i + 1}</kbd>` : ''}</button>`).join('');
  applyChrome();
}

function applyChrome() {
  const s = st.settings;
  document.documentElement.dataset.theme = s.theme || 'system';
  document.documentElement.dataset.density = s.density || 'comfortable';
  document.documentElement.dataset.motion = s.reduced_motion ? 'reduced' : 'normal';
  document.body.dataset.professional = s.professional_mode ? '1' : '0';
}

function go(view) {
  st.view = view;
  if (view === 'focus') st.focusIndex = 0;
  render();
  el('main').focus();
}

// Countdowns run on a local timer: exact, offline, never guessed.
setInterval(() => {
  document.querySelectorAll('[data-starts]').forEach((node) => {
    node.textContent = countdown(node.dataset.starts);
  });
}, 1000);

// ── Data ──────────────────────────────────────────────────────────────────

async function refresh(attempt = 0) {
  const boot = await call('bootstrap');
  if (!boot) {
    // The first call can land before the bridge can deliver a result, and a single
    // failure used to leave the window on its static HTML for good. Retrying is safe
    // because bootstrap only reads.
    if (attempt < 8) setTimeout(() => refresh(attempt + 1), 600);
    return;
  }
  st.rows = boot.items || [];
  st.sources = boot.sources || [];
  st.shortcuts = boot.shortcuts || [];
  st.settings = boot.settings || {};
  st.log = boot.action_log || [];
  st.hotkeys = boot.hotkeys || {};
  st.schedule = boot.schedule || {};
  st.voice = boot.voice || '';
  st.senders = boot.senders || [];
  st.privacy = boot.privacy || st.privacy || {};
  st.search = boot.search || st.search || {};
  st.summaries = boot.summaries || st.summaries || {};
  st.realtime = boot.realtime || null;
  applyPrivacy();
  render();
}

function applyPrivacy() {
  const p = st.privacy || {};
  document.body.dataset.privacy = p.on ? '1' : '0';
  const banner = el('privacy-banner');
  if (banner) {
    banner.querySelector('span').textContent = p.reason
      ? `${p.reason}. Press Ctrl+Shift+\ to show the panel again.`
      : 'Privacy mode is on. Press Ctrl+Shift+\ to show the panel again.';
  }
}

on('summary_ready', async (p) => {
  st.summaries = (await call('summaries')) || st.summaries;
  render();
  console.info(`[helm] generated: ${(p.due || []).join(', ')}`);
});
on('privacy_changed', (p) => { st.privacy = p; applyPrivacy(); render(); });
on('flag_changed', refresh);
on('item_changed', refresh);
on('source_status', refresh);
on('meeting_in_5', async (p) => {
  // Meetings are allowed to interrupt; everything else only moves a badge. But not
  // over a shared screen, a fullscreen app, or during quiet hours — Python decides,
  // because it is the side that can see the foreground window.
  const allowed = await call('may_interrupt');
  if (allowed && !allowed.ok) {
    console.info(`[helm] held back a meeting alert: ${allowed.reason}`);
    return;
  }
  call('open_panel');
  go('today');
  const note = document.createElement('div');
  note.className = 'pill pill--alert';
  note.style.cssText = 'position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:95';
  note.textContent = `${p.subject || 'Meeting'} starts in 5 minutes`;
  document.body.appendChild(note);
  setTimeout(() => note.remove(), 20_000);
});

on('action_logged', async () => { st.log = (await call('action_log')) || st.log; render(); });

// ── Tooltips ──────────────────────────────────────────────────────────────

const tip = el('tooltip');

function showTip(html, anchor) {
  tip.innerHTML = html;
  tip.dataset.open = '1';
  const r = anchor.getBoundingClientRect();
  const w = tip.offsetWidth;
  tip.style.left = `${Math.min(window.innerWidth - w - 10, Math.max(8, r.left))}px`;
  tip.style.top = `${r.bottom + 8}px`;
}

function hideTip() { tip.dataset.open = '0'; }

document.addEventListener('mouseover', (e) => {
  const badge = e.target.closest('.badge-pair');
  if (badge) {
    const r = st.rows.find((x) => x.item.id === badge.dataset.flagFor);
    if (r) showTip(V.tooltipHtml(r.flag), badge);
    return;
  }
  const warn = e.target.closest('[data-tip-warn]');
  if (warn) {
    const r = st.rows.find((x) => x.item.id === warn.dataset.tipWarn);
    if (r) showTip(`<h5>Sender warning<span>display only</span></h5>
      <ul><li>${esc(r.flag.posture_warning)}</li></ul>
      <div class="tt-foot">Flags were set from sender and timing alone.</div>`, warn);
    return;
  }
  const src = e.target.closest('[data-tip-source]');
  if (src) {
    const s = st.sources.find((x) => x.source === src.dataset.tipSource);
    if (s) showTip(`<h5>${esc(s.source)}<span>${esc(s.health)}</span></h5>
      <ul><li>last successful sync: ${esc(s.last_ok_at || 'never')}</li>
      <li>measured round-trip: ${s.measured_latency_ms} ms</li></ul>
      <div class="tt-foot">Demo mode reads fixtures, so this is not a network latency.</div>`,
    src);
    return;
  }
  hideTip();
});

// ── The item drawer ───────────────────────────────────────────────────────

const CHIPS = {
  mail: ['draft reply', 'draft follow-up', 'summarize', 'action items', 'create task'],
  slack: ['draft reply', 'summarize', 'action items', 'post summary to Slack'],
  calendar: ['prep brief', 'summarize', 'propose new time', 'remind me'],
};

/** A thread that costs a connector call shows a labelled skeleton, never a spinner
 *  that implies it is nearly done. */
function renderThread(d) {
  if (d.thread.length) {
    return d.thread.map((m) => `<div class="msg"><b>${esc(m.author)}</b>
      <time>${esc((m.sent_at || '').replace('T', ' ').slice(0, 16))}</time>
      <p>${esc(m.body)}</p></div>`).join('');
  }
  if (d.thread_loading) {
    return `<div class="msg"><p>${esc(d.item.snippet)}</p></div>
      <div class="msg" data-thread-skeleton="1">
        <b>loading the rest of the thread</b>
        <time>a connector call, about 1-2 minutes</time>
        <div class="skeleton" style="width:82%;margin:8px 0"></div>
        <div class="skeleton" style="width:64%;margin-bottom:6px"></div>
        <div class="skeleton" style="width:71%"></div>
      </div>`;
  }
  return `<div class="msg"><p>${esc(d.item.snippet)}</p></div>`;
}

async function openItem(itemId) {
  const d = await call('item_detail', itemId);
  if (!d || d.error) return;
  st.openItem = itemId;
  st.detail = d;

  const { item, flag, thread, attached, chat } = d;
  el('detail').innerHTML = `
    <h3>${esc(item.subject)}</h3>
    <div class="d-from">${esc(item.sender)} · ${esc(item.sender_addr)}
      ${item.sender_external ? ' · <b>external</b>' : ''}
      · ${item.to_me_direct ? 'to: you' : 'cc only'}</div>
    ${flag.posture_warning ? `<div class="d-warn">⚠ ${esc(flag.posture_warning)}</div>` : ''}
    ${d.needs_reply_reason ? `<div class="d-reason"><b>needs your reply</b> — ${
    esc(d.needs_reply_reason)}</div>` : ''}
    <div class="d-flags">
      <span class="pill pill--waiting">${QUADRANT_LABEL[flag.quadrant] || flag.quadrant}</span>
      <button class="btn btn--ghost" data-override="not_important"
        data-item-id="${esc(item.id)}">not important</button>
      <button class="btn btn--ghost" data-override="more_urgent"
        data-item-id="${esc(item.id)}">more urgent ↑</button>
    </div>
    <div id="threadbox">${renderThread(d)}</div>
    <div class="chips">${(CHIPS[item.source] || []).map((c) => `<button class="chip"
      data-chip="${esc(c)}">${esc(c)}</button>`).join('')}</div>`;

  el('attached').innerHTML = `<h6>attached to this chat</h6>${
    attached.map((a) => `<div><span class="${a.included ? 'yes' : 'no'}">
      ${a.included ? '✓' : '✗'}</span><span>${esc(a.label)}</span></div>`).join('')}`;

  el('chatlog').innerHTML = chat.map((m) => `<div class="bubble ${esc(m.role)}">
    ${esc(m.content)}</div>`).join('');
  el('drawer').dataset.open = '1';
  el('chatinput').focus();
}

function closeDrawer() {
  el('drawer').dataset.open = '0';
  st.openItem = null;
  st.detail = null;
}

// ── Chat streaming + the approval card ────────────────────────────────────

let streamNode = null;

window.helm = Object.assign(window.helm || {}, {
  onChatToken(itemId, token) {
    if (itemId !== st.openItem) return;
    if (!streamNode) {
      streamNode = document.createElement('div');
      streamNode.className = 'bubble assistant';
      streamNode.dataset.streaming = '1';
      el('chatlog').appendChild(streamNode);
    }
    streamNode.textContent += token;
    el('chatlog').scrollTop = el('chatlog').scrollHeight;
  },
  onChatDone(itemId, card) {
    if (streamNode) streamNode.dataset.streaming = '0';
    streamNode = null;
    if (itemId === st.openItem && card) renderApproval(card);
  },
  onThreadLoaded(itemId, messages, note) {
    if (itemId !== st.openItem || !st.detail) return;
    st.detail.thread = messages || [];
    st.detail.thread_loading = false;
    const box = el('threadbox');
    if (box) box.innerHTML = renderThread(st.detail)
      + (note ? `<div class="msg"><b>${esc(note)}</b></div>` : '');
  },
  focusAsk() { el('ask').focus(); },
  go,
  async panic() {
    st.privacy = (await call('set_privacy')) || st.privacy;
    applyPrivacy();
    render();
  },
});

function renderApproval(card) {
  const node = document.createElement('div');
  node.className = 'approval';
  node.innerHTML = `
    <header>Approval required · ${esc(card.risk)}</header>
    <dl>
      <dt>channel</dt><dd>${esc(card.channel)}</dd>
      <dt>to</dt><dd>${esc(card.to)}</dd>
      ${card.subject ? `<dt>subject</dt><dd>${esc(card.subject)}</dd>` : ''}
    </dl>
    <div class="body" contenteditable="true" spellcheck="false">${esc(card.body)}</div>
    <footer>
      <button class="btn btn--primary" data-approve="${esc(card.draft_id)}">Send</button>
      <button class="btn" data-edit-card="1">Edit</button>
      <button class="btn btn--ghost" data-discard="1">Discard</button>
    </footer>`;
  el('chatlog').appendChild(node);
  // Scroll after layout, not before. Setting scrollTop in the same frame left the
  // Send button below the fold, and scrollIntoView alone did not move the log
  // either — so move the scroll container itself, once the card has a height.
  requestAnimationFrame(() => {
    const log = el('chatlog');
    log.scrollTop = log.scrollHeight;
  });
}

/** The click above is the approval. The delay below is the window to take it back. */
function startDelaySend(card, draftId) {
  let left = UNDO_SECONDS;
  card.querySelector('footer').remove();
  const bar = document.createElement('div');
  bar.className = 'undo';
  card.appendChild(bar);

  const paint = () => { bar.innerHTML = `Sending in ${left}s. <button class="btn btn--ghost"
    data-undo="1">Undo</button>`; };
  paint();

  const timer = setInterval(async () => {
    left -= 1;
    if (left > 0) { paint(); return; }
    clearInterval(timer);
    const res = await call('approve_send', draftId);
    bar.textContent = res?.demo
      ? 'Demo mode — the approval gate ran, nothing was actually sent.'
      : (res?.ok ? 'Sent.' : 'Send failed.');
  }, 1000);

  bar.addEventListener('click', (e) => {
    if (!e.target.closest('[data-undo]')) return;
    clearInterval(timer);
    bar.textContent = 'Cancelled. Nothing was sent.';
  });
}

// ── Clicks ────────────────────────────────────────────────────────────────

document.addEventListener('click', async (e) => {
  const t = e.target;

  const nav = t.closest('[data-view]');
  if (nav) return go(nav.dataset.view);

  const filter = t.closest('[data-filter]');
  if (filter) { st.filter = filter.dataset.filter; return render(); }

  const row = t.closest('[data-item]');
  if (row) return openItem(row.dataset.item);

  const join = t.closest('[data-join]');
  if (join) return call('open_shortcut', join.dataset.join);

  const prep = t.closest('[data-prep]');
  if (prep) {
    await openItem(prep.dataset.prep);
    return sendChat('prep brief');
  }

  const url = t.closest('[data-open-url]');
  if (url) return call('open_shortcut', url.dataset.openUrl);

  const ov = t.closest('[data-override]');
  if (ov) {
    await call('flag_override', ov.dataset.itemId, ov.dataset.override);
    await refresh();
    if (st.openItem) await openItem(st.openItem);
    return undefined;
  }

  const chip = t.closest('[data-chip]');
  if (chip) return sendChat(chip.dataset.chip);

  const approve = t.closest('[data-approve]');
  if (approve) return startDelaySend(approve.closest('.approval'), approve.dataset.approve);

  if (t.closest('[data-edit-card]')) {
    return t.closest('.approval').querySelector('.body').focus();
  }
  if (t.closest('[data-discard]')) return t.closest('.approval').remove();

  const focusAct = t.closest('[data-focus-act]');
  if (focusAct) return doFocusAction(focusAct.dataset.focusAct);

  const delSc = t.closest('[data-del-sc]');
  if (delSc) {
    const res = await call('delete_shortcut', Number(delSc.dataset.delSc));
    if (res) st.shortcuts = res.shortcuts;
    return render();
  }

  const commitDone = t.closest('[data-commit-done]');
  if (commitDone) {
    const res = await call('set_commitment_state',
                           Number(commitDone.dataset.commitDone), 'done');
    if (res) st.summaries = { ...st.summaries, commitments: res.commitments };
    return render();
  }

  if (t.closest('#refresh-commitments')) {
    const res = await call('refresh_commitments');
    if (res) st.summaries = { ...st.summaries, commitments: res.commitments };
    return render();
  }

  if (t.closest('#reindex')) {
    const res = await call('reindex');
    if (res) st.search = { ...st.search, indexed: res.indexed };
    return render();
  }

  if (t.closest('#privacy-toggle')) {
    st.privacy = (await call('set_privacy')) || st.privacy;
    applyPrivacy();
    return render();
  }

  if (t.closest('#sc-add')) return addShortcut();
  if (t.closest('#sc-export')) {
    const json = await call('export_shortcuts');
    el('sc-msg').textContent = json ? 'Copied below — save it anywhere.' : '';
    if (json) el('sc-msg').innerHTML += `<pre style="white-space:pre-wrap">${esc(json)}</pre>`;
    return undefined;
  }
  if (t.closest('#sc-import')) {
    const json = el('sc-msg').querySelector('textarea')?.value;
    if (!json) {
      el('sc-msg').innerHTML = `<textarea rows="5" style="width:100%" placeholder="paste JSON">
</textarea><div>Paste, then press Import again.</div>`;
      return undefined;
    }
    const res = await call('import_shortcuts', json);
    if (res?.shortcuts) st.shortcuts = res.shortcuts;
    render();
    el('sc-msg').textContent = res ? `Added ${res.added}, skipped ${res.skipped}.` : '';
    return undefined;
  }

  if (t.closest('#resync')) {
    const btn = t.closest('#resync');
    btn.textContent = 'Refreshing…';
    await call('resync');
    await refresh();
    return undefined;
  }

  const forget = t.closest('[data-forget]');
  if (forget) {
    const res = await call('forget_sender', forget.dataset.forget);
    if (res) await refresh();
    return undefined;
  }

  if (t.closest('#classify-now')) {
    el('classify-msg').textContent = 'Asking about the unclear ones… about 2 minutes.';
    const res = await call('classify_ambiguous');
    el('classify-msg').textContent = res?.error
      ? res.error
      : res?.skipped || res?.note
        || `Asked about ${res.asked}, got ${res.answered} back, ${res.changed} changed.`;
    await refresh();
    return undefined;
  }

  if (t.closest('#rebuild-voice')) {
    el('voice-msg').textContent = 'Reading a few of your sent messages… about 2 minutes.';
    const res = await call('rebuild_voice');
    el('voice-msg').textContent = res?.described || res?.error || 'Could not rebuild.';
    await refresh();
    return undefined;
  }

  if (t.closest('#demo-reset')) {
    await call('reset_demo');
    return refresh();
  }
  if (t.closest('#show-firstrun')) return el('firstrun').setAttribute('data-open', '1');
  if (t.closest('#firstrun-done')) {
    el('firstrun').dataset.open = '0';
    return call('set_setting', 'first_run_done', true);
  }

  if (t.closest('#btn-help')) return el('help').setAttribute('data-open', '1');
  if (t.closest('#btn-settings')) return go('settings');
  if (t.closest('#chat-close')) return closeDrawer();

  const overlay = t.closest('.overlay');
  if (overlay && t === overlay) overlay.dataset.open = '0';
  if (t === el('drawer')) closeDrawer();
  return undefined;
});

async function addShortcut() {
  const label = el('sc-label').value.trim();
  const url = el('sc-url').value.trim();
  const group = el('sc-group').value.trim() || 'Work';
  if (!label || !url) { el('sc-msg').textContent = 'Both a label and a URL are needed.'; return; }
  const res = await call('add_shortcut', label, url, group);
  if (res?.error) { el('sc-msg').textContent = res.error; return; }
  if (res?.shortcuts) st.shortcuts = res.shortcuts;
  render();
}

// ── Settings inputs ───────────────────────────────────────────────────────

document.addEventListener('change', async (e) => {
  const t = e.target;

  const key = t.dataset.set;
  if (key) {
    const value = t.type === 'checkbox' ? t.checked : t.value;
    st.settings = (await call('set_setting', key, value)) || st.settings;
    applyChrome();
    if (key === 'density' || key === 'theme') render();
    return;
  }

  const listKey = t.dataset.setList;
  if (listKey) {
    const value = t.value.split(',').map((x) => x.trim()).filter(Boolean);
    st.settings = (await call('set_setting', listKey, value)) || st.settings;
    await refresh();
    return;
  }

  const timeKey = t.dataset.setTime;
  if (timeKey) {
    const [group, field] = timeKey.split('.');
    const next = { ...(st.settings[group] || {}), [field]: t.value };
    st.settings = (await call('set_setting', group, next)) || st.settings;
  }
});

document.addEventListener('input', (e) => {
  if (e.target.id === 'q') runSearch(e.target.value);
});

let searchTimer = null;

function runSearch(text) {
  st.query = text;
  clearTimeout(searchTimer);
  // Each keystroke would otherwise be its own bridge round-trip.
  searchTimer = setTimeout(async () => {
    const token = ++st.queryToken;
    const res = await call('search', st.query);
    // A slower earlier query must not overwrite the newest results.
    if (token !== st.queryToken) return;
    st.results = res?.results || [];
    if (res) st.search = { available: res.available, indexed: res.indexed };
    redrawSearch();
  }, 120);
  redrawSearch();
}

/** Repaint without losing the caret: the input is the thing being typed into. */
function redrawSearch() {
  if (st.view !== 'search') return;
  const main = el('main');
  const pos = main.scrollTop;
  const caret = el('q')?.selectionStart ?? null;
  render();
  main.scrollTop = pos;
  const q = el('q');
  if (q) {
    q.focus();
    const at = caret === null ? q.value.length : caret;
    q.setSelectionRange(at, at);
  }
}

// ── Chat send ─────────────────────────────────────────────────────────────

function sendChat(text) {
  if (!st.openItem || !text.trim()) return;
  const node = document.createElement('div');
  node.className = 'bubble user';
  node.textContent = text;
  el('chatlog').appendChild(node);
  el('chatlog').scrollTop = el('chatlog').scrollHeight;
  call('chat_send', st.openItem, text);
}

el('chatform').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = el('chatinput');
  sendChat(input.value);
  input.value = '';
});

// ── Ask bar ───────────────────────────────────────────────────────────────

el('ask').addEventListener('keydown', async (e) => {
  if (e.key !== 'Enter') return;
  const text = el('ask').value.trim();
  if (!text) return;
  el('ask').value = '';

  // "open <name>" resolves against the shortcut list before anything else.
  const m = /^open\s+(.+)$/i.exec(text);
  if (m) {
    const needle = m[1].toLowerCase();
    const hit = st.shortcuts.find((s) => s.label.toLowerCase().includes(needle));
    if (hit) return call('open_shortcut', hit.url);
  }

  const view = V.NAV.find(([key, label]) => text.toLowerCase() === key
    || text.toLowerCase() === label.toLowerCase());
  if (view) return go(view[0]);

  go('search');
  runSearch(text);
  return undefined;
});

// ── Focus actions ─────────────────────────────────────────────────────────

async function doFocusAction(action) {
  const queue = V.focusRows(st.rows);
  const current = queue[Math.min(st.focusIndex, queue.length - 1)];
  if (!current) return;

  // `r` on a waiting item means chase it, not answer it — nobody has replied yet.
  if (action === 'followup' || (action === 'reply' && current.item.state === 'waiting')) {
    await openItem(current.item.id);
    return sendChat('draft a follow-up — no reply since I sent this');
  }
  if (action === 'reply') {
    await openItem(current.item.id);
    return sendChat('draft reply');
  }
  if (action === 'delegate') {
    await openItem(current.item.id);
    return sendChat('draft reply — hand this to the right owner');
  }
  const state = action === 'done' ? 'done' : 'snoozed';
  await call('set_item_state', current.item.id, state);
  await refresh();
  return undefined;
}

// ── Keyboard ──────────────────────────────────────────────────────────────

const TYPING = new Set(['INPUT', 'TEXTAREA']);

document.addEventListener('keydown', (e) => {
  const typing = TYPING.has(e.target.tagName) || e.target.isContentEditable;

  if (e.key === 'Escape') {
    if (el('drawer').dataset.open === '1') return closeDrawer();
    document.querySelectorAll('.overlay[data-open="1"]')
      .forEach((o) => { o.dataset.open = '0'; });
    return undefined;
  }

  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    return el('ask').focus();
  }

  if (e.altKey && /^[1-9]$/.test(e.key)) {
    e.preventDefault();
    const s = st.shortcuts[Number(e.key) - 1];
    if (s) call('open_shortcut', s.url);
    return undefined;
  }

  if (typing) return undefined;

  if (e.key === '?') { el('help').dataset.open = '1'; return undefined; }

  if (st.view === 'focus') {
    const queue = V.focusRows(st.rows);
    if (e.key === 'j') { st.focusIndex = Math.min(queue.length - 1, st.focusIndex + 1); return render(); }
    if (e.key === 'k') { st.focusIndex = Math.max(0, st.focusIndex - 1); return render(); }
    if ('resd'.includes(e.key)) {
      return doFocusAction({ r: 'reply', e: 'done', s: 'snooze', d: 'delegate' }[e.key]);
    }
  }

  const index = V.NAV.findIndex(([, label]) => label[0].toLowerCase() === e.key);
  if (index >= 0 && !e.ctrlKey && !e.metaKey) return go(V.NAV[index][0]);
  return undefined;
});

// ── Keyboard help content ─────────────────────────────────────────────────

function renderKeys() {
  const groups = [
    ['Global', [
      ['Ctrl+Shift+H', 'show the panel from anywhere'],
      ['Ctrl/⌘+K', 'ask bar'],
      ['Ctrl+Shift+F', 'jump to the focus queue'],
      ['Ctrl+Shift+\\', 'panic — blur everything'],
      ['Alt+1…9', 'open shortcut 1 to 9'],
      ['?', 'this list'],
      ['Esc', 'close the drawer or an overlay'],
    ]],
    ['Navigation', V.NAV.map(([, label]) => [label[0].toLowerCase(), label.toLowerCase()])],
    ['Focus queue', [
      ['j', 'next item'],
      ['k', 'previous item'],
      ['r', 'reply — drafts, never sends'],
      ['e', 'done'],
      ['s', 'snooze'],
      ['d', 'delegate'],
    ]],
  ];
  el('keys').innerHTML = groups.map(([name, pairs]) => `<section><h4>${esc(name)}</h4>
    <dl>${pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
    </section>`).join('');
}

// ── Boot ──────────────────────────────────────────────────────────────────

ready.then(async () => {
  await refresh();
  renderKeys();
  if (!st.settings.first_run_done) el('firstrun').dataset.open = '1';
});
