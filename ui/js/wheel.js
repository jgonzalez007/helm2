// HELM's character: a ship's helm wheel, drawn procedurally on a canvas.
// Original artwork — nothing borrowed. Four states, matching the spec's colours:
// amber = waiting on you, blue = working, green = done, neutral = idle.

const COLORS = {
  idle: '#8e97a5',
  waiting: '#e8962c',
  working: '#7ea6ff',
  done: '#4ec99b',
  alert: '#f08a8a',
};

export class Wheel {
  constructor(canvas) {
    this.c = canvas;
    this.ctx = canvas.getContext('2d');
    this.state = 'idle';
    this.spin = 0;
    this.t = 0;
    this.muted = false;
    this.reduced = false;
    this.raf = null;
  }

  setState(state) {
    if (state in COLORS) this.state = state;
  }

  start() {
    if (this.raf !== null) return;
    const tick = () => {
      this.t += 1;
      // Working spins; everything else breathes. Reduced motion holds still.
      if (!this.reduced) this.spin += this.state === 'working' ? 0.045 : 0.004;
      this.draw();
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }

  stop() {
    if (this.raf !== null) cancelAnimationFrame(this.raf);
    this.raf = null;
  }

  draw() {
    const { ctx, c } = this;
    const s = c.width;
    const r = s * 0.36;
    const cx = s / 2;
    const cy = s / 2;
    const color = COLORS[this.state];
    // Breathing is a 2% radius wobble — present enough to feel alive, small
    // enough not to pull the eye during a meeting.
    const breathe = this.reduced ? 1 : 1 + Math.sin(this.t / 34) * 0.02;

    ctx.clearRect(0, 0, s, s);
    ctx.save();
    ctx.globalAlpha = this.muted ? 0.4 : 1;
    ctx.translate(cx, cy);
    ctx.rotate(this.spin);
    ctx.scale(breathe, breathe);

    // Glow, except when idle or muted.
    if (this.state !== 'idle' && !this.muted) {
      ctx.shadowColor = color;
      ctx.shadowBlur = s * 0.18;
    }

    ctx.strokeStyle = color;
    ctx.lineWidth = Math.max(1.6, s * 0.055);
    ctx.beginPath();
    ctx.arc(0, 0, r, 0, Math.PI * 2);
    ctx.stroke();

    ctx.shadowBlur = 0;
    ctx.lineWidth = Math.max(1.3, s * 0.042);
    for (let i = 0; i < 4; i += 1) {
      const a = (i * Math.PI) / 2;
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * r * 0.25, Math.sin(a) * r * 0.25);
      ctx.lineTo(Math.cos(a) * r * 1.3, Math.sin(a) * r * 1.3);
      ctx.stroke();
    }

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(0, 0, r * 0.3, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }
}

/** The single state the character should show, given everything on screen. */
export function wheelStateFor(rows) {
  const flags = rows.map((r) => r.flag || {});
  if (flags.some((f) => f.quadrant === 'important_urgent')) return 'waiting';
  const items = rows.map((r) => r.item || {});
  if (items.some((i) => i.state === 'needs_me')) return 'waiting';
  if (items.some((i) => i.state === 'working')) return 'working';
  if (items.length && items.every((i) => i.state === 'done')) return 'done';
  return 'idle';
}
