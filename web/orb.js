// Voice orb: a soft MTN-yellow blob drawn on a canvas.
// idle: slow breathing · listening: swells with the user's voice · thinking: a shimmer circles it ·
// speaking: pulses with the assistant's audio.
const STATES = {
  idle:      { amp: 0.025, speed: 0.5, gain: 0.0,  glow: 0.25 },
  listening: { amp: 0.035, speed: 1.0, gain: 0.22, glow: 0.45 },
  thinking:  { amp: 0.05,  speed: 1.8, gain: 0.0,  glow: 0.4 },
  speaking:  { amp: 0.04,  speed: 1.4, gain: 0.28, glow: 0.55 },
};

export class Orb {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.state = "idle";
    this.mix = { ...STATES.idle };
    this.level = 0;
    this.target = 0;
    this.t = 0;
    this.last = performance.now();
    this.reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.ring = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#000";
    new ResizeObserver(() => this.resize()).observe(canvas);
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
      this.ring = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#000";
    });
    this.resize();
    this.frame = this.frame.bind(this);
    requestAnimationFrame(this.frame);
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const { width, height } = this.canvas.getBoundingClientRect();
    this.w = width;
    this.h = height;
    this.canvas.width = Math.max(1, Math.round(width * dpr));
    this.canvas.height = Math.max(1, Math.round(height * dpr));
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  setState(state) { this.state = STATES[state] ? state : "idle"; }

  // 0..1; peaks decay, so short bursts of sound read as pulses.
  setLevel(v) { this.target = Math.max(this.target, Math.min(1, Math.max(0, v))); }

  blob(cx, cy, r, amp, phase, wobble) {
    const ctx = this.ctx;
    const n = 96;
    ctx.beginPath();
    for (let i = 0; i <= n; i++) {
      const a = (i / n) * Math.PI * 2;
      const k = 1 + amp * (
        0.55 * Math.sin(3 * a + this.t * 1.1 + phase) +
        0.35 * Math.sin(5 * a - this.t * 1.7 + phase * 2) +
        0.25 * Math.sin(2 * a + this.t * 0.7 + wobble)
      );
      const x = cx + Math.cos(a) * r * k;
      const y = cy + Math.sin(a) * r * k;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.closePath();
  }

  frame(now) {
    requestAnimationFrame(this.frame);
    if (!this.w || document.hidden || this.canvas.offsetParent === null) { this.last = now; return; }
    const dt = Math.min(0.05, (now - this.last) / 1000);
    this.last = now;

    // Ease state parameters and the sound level.
    const goal = STATES[this.state];
    for (const k in goal) this.mix[k] += (goal[k] - this.mix[k]) * Math.min(1, dt * 4);
    this.level += (this.target - this.level) * Math.min(1, dt * 14);
    this.target *= Math.pow(0.02, dt); // decay
    const m = this.mix;
    this.t += dt * (this.reduced ? 0.15 : m.speed);

    const ctx = this.ctx;
    const { w, h } = this;
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2;
    const base = Math.min(w, h) * 0.3;
    const breathe = 1 + (this.reduced ? 0 : 0.025 * Math.sin(this.t * 1.3));
    const r = base * breathe * (1 + m.gain * this.level);
    const amp = (this.reduced ? 0.01 : m.amp) + m.gain * this.level * 0.35;

    // Glow
    const glowR = r * (1.55 + 0.25 * this.level);
    const g = ctx.createRadialGradient(cx, cy, r * 0.6, cx, cy, glowR);
    g.addColorStop(0, `rgba(255, 203, 5, ${0.35 * m.glow + 0.25 * this.level})`);
    g.addColorStop(1, "rgba(255, 203, 5, 0)");
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(cx, cy, glowR, 0, Math.PI * 2);
    ctx.fill();

    // Deep amber base layer
    this.blob(cx, cy, r * 1.04, amp * 1.2, 0.0, 1.3);
    let grad = ctx.createRadialGradient(cx, cy, r * 0.2, cx, cy, r * 1.1);
    grad.addColorStop(0, "#ffd84a");
    grad.addColorStop(1, "#f2a900");
    ctx.fillStyle = grad;
    ctx.fill();

    // MTN yellow body
    this.blob(cx, cy, r * 0.94, amp, 2.1, 0.4);
    grad = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.35, r * 0.05, cx, cy, r);
    grad.addColorStop(0, "#fff6c8");
    grad.addColorStop(0.45, "#ffcb05");
    grad.addColorStop(1, "#f6b800");
    ctx.fillStyle = grad;
    ctx.fill();

    // Soft highlight
    this.blob(cx - r * 0.22, cy - r * 0.28, r * 0.42, amp * 1.6, 4.2, 2.2);
    grad = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.36, 0, cx - r * 0.22, cy - r * 0.28, r * 0.5);
    grad.addColorStop(0, "rgba(255, 255, 255, 0.75)");
    grad.addColorStop(1, "rgba(255, 255, 255, 0)");
    ctx.fillStyle = grad;
    ctx.fill();

    // Thinking: a shimmer arc circling the orb
    if (this.state === "thinking") {
      const a0 = this.t * 2.2;
      ctx.lineWidth = Math.max(2, r * 0.035);
      ctx.lineCap = "round";
      ctx.strokeStyle = this.ring;
      ctx.globalAlpha = 0.85;
      ctx.beginPath();
      ctx.arc(cx, cy, r * 1.28, a0, a0 + Math.PI * 0.55);
      ctx.stroke();
      ctx.globalAlpha = 0.35;
      ctx.beginPath();
      ctx.arc(cx, cy, r * 1.28, a0 + Math.PI, a0 + Math.PI * 1.25);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Idle: slightly muted until the call starts
    if (this.state === "idle") {
      ctx.globalCompositeOperation = "source-atop";
      ctx.fillStyle = "rgba(255, 255, 255, 0.18)";
      ctx.fillRect(0, 0, w, h);
      ctx.globalCompositeOperation = "source-over";
    }
  }
}
