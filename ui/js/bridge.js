// The pywebview bridge, wrapped so a missing backend degrades instead of throwing.
// Opening a .html file straight in a browser gives you the UI with empty data,
// which is useful for styling work and useless for anything else.

let resolveReady;
export const ready = new Promise((r) => { resolveReady = r; });

const hasApi = () => Boolean(window.pywebview?.api);

window.addEventListener('pywebviewready', () => resolveReady(true));
// If the backend was already injected before this module ran, the event is gone.
if (hasApi()) resolveReady(true);

// A window created hidden only loads once it is shown, and injection can take
// several seconds after that. Poll rather than give up on a fixed timer — an
// early "no backend" verdict is permanent and leaves the panel blank.
const started = Date.now();
const poll = setInterval(() => {
  if (hasApi()) { clearInterval(poll); resolveReady(true); }
  else if (Date.now() - started > 20_000) { clearInterval(poll); resolveReady(false); }
}, 100);

export async function call(method, ...args) {
  await ready;
  if (!window.pywebview?.api?.[method]) {
    console.warn(`[helm] no backend for ${method}()`);
    return null;
  }
  try {
    return await window.pywebview.api[method](...args);
  } catch (err) {
    console.error(`[helm] ${method}() failed`, err);
    return null;
  }
}

// Events arrive from Python via window.helm.* — see Api._push in helm/app.py.
const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, []);
  listeners.get(event).push(fn);
}

export function emit(event, payload) {
  for (const fn of listeners.get(event) || []) {
    try { fn(payload); } catch (err) { console.error(`[helm] handler for ${event}`, err); }
  }
}

// Surfaced so the window smoke test can tell "render threw" from "no data yet".
export const diag = { lastError: null };

window.addEventListener('error', (e) => { diag.lastError = String(e.message); });
window.addEventListener('unhandledrejection', (e) => {
  diag.lastError = String(e.reason?.message || e.reason);
});

window.helm = Object.assign(window.helm || {}, {
  onEvent: (event, payload) => emit(event, payload),
  diag,
});
