// PCM conversion runs on the audio rendering thread, away from React/Markdown.
class InterviewPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.pcm = new Int16Array(1024);
    this.used = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (let index = 0; index < input.length; index++) {
      const sample = Math.max(-1, Math.min(1, input[index]));
      this.pcm[this.used++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      if (this.used === this.pcm.length) {
        const pcm = this.pcm.buffer;
        this.port.postMessage({ pcm, endTime: currentTime + (index + 1) / sampleRate }, [pcm]);
        this.pcm = new Int16Array(1024);
        this.used = 0;
      }
    }
    return true;
  }
}

registerProcessor("interview-pcm", InterviewPcmProcessor);
