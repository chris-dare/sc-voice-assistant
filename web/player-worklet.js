// Gapless playback of streamed PCM. The main thread pushes Float32 samples; "clear" stops at once.
class PlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunks = [];
    this.offset = 0;
    this.playing = false;
    this.levelAcc = 0;
    this.levelN = 0;
    this.port.onmessage = (e) => {
      const msg = e.data;
      if (msg.type === "push") {
        this.chunks.push(msg.samples);
      } else if (msg.type === "clear") {
        this.chunks = [];
        this.offset = 0;
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    let written = 0;
    while (written < out.length && this.chunks.length) {
      const chunk = this.chunks[0];
      const n = Math.min(out.length - written, chunk.length - this.offset);
      for (let i = 0; i < n; i++) {
        const v = chunk[this.offset + i];
        out[written + i] = v;
        this.levelAcc += v * v;
      }
      written += n;
      this.offset += n;
      this.levelN += n;
      if (this.offset >= chunk.length) {
        this.chunks.shift();
        this.offset = 0;
      }
    }
    for (let i = written; i < out.length; i++) out[i] = 0;
    const active = written > 0;
    if (active !== this.playing) {
      this.playing = active;
      this.port.postMessage({ type: active ? "playing" : "drained" });
    }
    if (this.levelN >= 2048) {
      this.port.postMessage({ type: "level", rms: Math.sqrt(this.levelAcc / this.levelN) });
      this.levelAcc = 0;
      this.levelN = 0;
    }
    return true;
  }
}

registerProcessor("player-processor", PlayerProcessor);
