// Island state machine + render. Hover peeks, dwell expands to compact,
// click opens the panel window.

import { call, on, ready } from './bridge.js';
import { countdown, clockTime, esc } from './fmt.js';
import { Wheel, wheelStateFor } from './wheel.js';

const HEIGHTS = { hidden: 8, peek: 28, compact: 36 };
const PEEK_TO_HIDDEN_MS = 2500;
const COMPACT_TO_PEEK_MS = 4000;

const el = {
  island: document.getElementById('island'),
  wake: document.getElementById('wake'),
  counts: document.getElementById('counts'),
  next: document.getElementById('next'),
  wheel: document.getElementById('wheel'),
};

const wheel = new Wheel(el.wheel);
let state = 'hidden';
let timer = null;
let rows = [];
let sources = [];

function setState(next) {
  if (next === state) return;
  state = next;
  el.island.dataset.state = next;
  call('resize_island', HEIGHTS[next]);
}

function later(ms, fn) {
  clearTimeout(timer);
  timer = setTimeout(fn, ms);
}

// ── FSM inputs ────────────────────────────────────────────────────────────

el.wake.addEventListener('mouseenter', () => {
  if (state === 'hidden') setState('peek');
});

el.island.addEventListener('mouseenter', () => {
  clearTimeout(timer);
  if (state === 'peek') setState('compact');
});

el.island.addEventListener('mouseleave', () => {
  if (state === 'compact') later(COMPACT_TO_PEEK_MS, () => setState('peek'));
  else if (state === 'peek') later(PEEK_TO_HIDDEN_MS, () => setState('hidden'));
});

el.island.addEventListener('click', () => call('open_panel'));
el.island.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    call('open_panel');
  }
});

// ── Render ────────────────────────────────────────────────────────────────

const ICONS = {
  mail: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">'
    + '<rect x="1.5" y="3.5" width="13" height="9" rx="1.5"/><path d="M2 5l6 4 6-4"/></svg>',
  slack: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">'
    + '<path d="M5 2v12M11 2v12M2 5.5h12M2 10.5h12"/></svg>',
};

function nextMeeting() {
  const upcoming = rows
    .filter((r) => r.item.source === 'calendar' && Date.parse(r.item.starts_at) > Date.now())
    .sort((a, b) => Date.parse(a.item.starts_at) - Date.parse(b.item.starts_at));
  return upcoming[0] || null;
}

function render() {
  const counts = {};
  for (const r of rows) {
    if (r.item.state !== 'needs_me') continue;
    counts[r.item.source] = (counts[r.item.source] || 0) + 1;
  }
  const hot = new Set(
    rows.filter((r) => r.flag.quadrant === 'important_urgent').map((r) => r.item.source),
  );

  el.counts.innerHTML = ['mail', 'slack'].map((src) => {
    const n = counts[src] || 0;
    return `<span class="isl-count" data-hot="${hot.has(src) ? 1 : 0}">`
      + `${ICONS[src]}<b>${n}</b></span>`;
  }).join('');

  const next = nextMeeting();
  if (next) {
    el.next.innerHTML = `<span>next</span><b data-starts="${esc(next.item.starts_at)}">`
      + `${countdown(next.item.starts_at)}</b>`
      + `<span class="isl-sub">${clockTime(next.item.starts_at)} `
      + `${esc(next.item.subject)}</span>`;
  } else {
    el.next.innerHTML = '<span class="isl-sub">no meetings ahead</span>';
  }

  wheel.setState(wheelStateFor(rows));
}

// The countdown runs on a local timer, so it is exact and works offline.
setInterval(() => {
  const b = el.next.querySelector('b[data-starts]');
  if (b) b.textContent = countdown(b.dataset.starts);
}, 1000);

// ── Boot ──────────────────────────────────────────────────────────────────

async function refresh() {
  const boot = await call('bootstrap');
  if (!boot) return;
  rows = boot.items || [];
  sources = boot.sources || [];
  const s = boot.settings || {};
  document.documentElement.dataset.theme = s.theme || 'system';
  document.documentElement.dataset.density = s.density || 'comfortable';
  document.body.dataset.professional = s.professional_mode ? '1' : '0';
  wheel.muted = Boolean(s.professional_mode);
  wheel.reduced = Boolean(s.reduced_motion);
  render();
}

on('source_status', refresh);
on('flag_changed', refresh);
on('item_changed', refresh);

window.helm = Object.assign(window.helm || {}, {
  panic() {
    document.body.dataset.privacy = document.body.dataset.privacy === '1' ? '0' : '1';
  },
});

ready.then(async () => {
  wheel.start();
  await refresh();
  // Announce itself once on launch, then get out of the way.
  setState('compact');
  later(COMPACT_TO_PEEK_MS, () => setState('peek'));
});
