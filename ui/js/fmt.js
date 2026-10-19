// Formatting shared by the island and the panel.

export const IMPORTANCE_GLYPH = { high: '◆', normal: '◇', low: '·' };
export const URGENCY_GLYPH = { now: '▲', today: '△', week: '▴', none: '·' };
export const IMPORTANCE_LABEL = { high: 'High', normal: 'Normal', low: 'Low' };
export const URGENCY_LABEL = { now: 'Now', today: 'Today', week: 'This week', none: 'None' };

export const QUADRANT_LABEL = {
  important_urgent: 'important + urgent',
  important_only: 'important, not urgent',
  urgent_only: 'urgent, not important',
  neither: 'neither',
};

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** Relative time, past or future, in the units a person would use. */
export function rel(iso, now = Date.now()) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const secs = Math.round((t - now) / 1000);
  const abs = Math.abs(secs);
  const unit = abs < 60 ? [secs, 's']
    : abs < 3600 ? [Math.round(secs / 60), 'm']
      : abs < 86400 ? [Math.round(secs / 3600), 'h']
        : [Math.round(secs / 86400), 'd'];
  const [n, u] = unit;
  return secs < 0 ? `${Math.abs(n)}${u}` : `in ${n}${u}`;
}

/** mm:ss or h:mm:ss for a meeting countdown. Driven by a local timer, so exact. */
export function countdown(iso, now = Date.now()) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  let secs = Math.round((t - now) / 1000);
  if (secs <= 0) return 'now';
  const h = Math.floor(secs / 3600);
  secs -= h * 3600;
  const m = Math.floor(secs / 60);
  const s = secs - m * 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function clockTime(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  return new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function longDate(d = new Date()) {
  return d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
}

export function greeting(d = new Date()) {
  const h = d.getHours();
  if (h < 12) return 'Morning';
  if (h < 18) return 'Afternoon';
  return 'Evening';
}

/** How old the data is, always stated.
 *
 * A polled source is never labelled "live", however recently it synced — HELM
 * refreshes every 2 to 5 minutes, and a word that implies otherwise is the one
 * lie that would make the whole panel untrustworthy. The dot carries connection
 * state; this text carries age. */
export function freshness(state) {
  if (!state) return 'no data';
  if (state.health === 'unavailable') return 'unavailable';
  if (state.health === 'demo') return 'demo data';
  if (!state.last_ok_at) {
    return state.health === 'reconnecting' ? 'reconnecting' : 'never synced';
  }
  const age = Date.now() - Date.parse(state.last_ok_at);
  if (Number.isNaN(age)) return 'unknown';
  const stamp = age < 60_000 ? 'just now' : `${rel(state.last_ok_at)} ago`;
  // A failing source still shows the age of what you are looking at.
  return state.health === 'reconnecting' ? `reconnecting · ${stamp}` : stamp;
}

export function badgePair(flag) {
  const i = flag.importance || 'normal';
  const u = flag.urgency || 'none';
  return `<span class="badge-pair" data-flag-for="${esc(flag.item_id)}">`
    + `<span class="badge badge--imp-${i}" role="img"`
    + ` aria-label="Importance ${IMPORTANCE_LABEL[i]}">${IMPORTANCE_GLYPH[i]}</span>`
    + `<span class="badge badge--urg-${u}" role="img"`
    + ` aria-label="Urgency ${URGENCY_LABEL[u]}">${URGENCY_GLYPH[u]}</span>`
    + '</span>';
}
