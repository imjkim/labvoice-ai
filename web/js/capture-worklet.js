// Runs on the audio thread: batches microphone samples into 20 ms frames for the main thread.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Float32Array(Math.round(sampleRate * 0.02)); // `sampleRate` is a worklet global
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      let i = 0;
      while (i < channel.length) {
        const n = Math.min(channel.length - i, this.frame.length - this.filled);
        this.frame.set(channel.subarray(i, i + n), this.filled);
        this.filled += n;
        i += n;
        if (this.filled === this.frame.length) {
          this.port.postMessage(this.frame.slice(0));
          this.filled = 0;
        }
      }
    }
    return true; // keep running
  }
}

registerProcessor("capture", CaptureProcessor);
