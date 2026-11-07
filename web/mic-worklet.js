// Captures mic audio, resamples to 16 kHz mono PCM16 and posts 20 ms frames with their RMS level.
class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;
    this.frame = new Int16Array(320);
    this.fill = 0;
    this.sumSq = 0;
    this.prev = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    // Linear-interpolation resampler from the context rate to 16 kHz.
    let pos = this.pos;
    while (pos < input.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const a = i === 0 ? this.prev : input[i - 1];
      const b = input[i];
      const s = Math.max(-1, Math.min(1, a + (b - a) * frac));
      this.frame[this.fill++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      this.sumSq += s * s;
      if (this.fill === this.frame.length) {
        const rms = Math.sqrt(this.sumSq / this.frame.length);
        const out = this.frame.slice(0);
        this.port.postMessage({ pcm: out.buffer, rms }, [out.buffer]);
        this.fill = 0;
        this.sumSq = 0;
      }
      pos += this.ratio;
    }
    this.pos = pos - input.length;
    this.prev = input[input.length - 1];
    return true;
  }
}

registerProcessor("mic-processor", MicProcessor);
