import { createUtteranceSegmenter } from '../src/voiceTranscribe.ts';

function assert(cond: boolean, msg: string): void {
  if (!cond) throw new Error(msg);
}

const rate = 1000; // 1 sample = 1 ms
const silent = createUtteranceSegmenter(rate);

function frame(n: number, value: number): Float32Array {
  const out = new Float32Array(n);
  out.fill(value);
  return out;
}

for (let i = 0; i < 20; i++) {
  assert(silent.push(frame(50, 0)) === null, 'silence must not emit');
}
assert(silent.flush() === null, 'flush of silence is empty');

const segmenter = createUtteranceSegmenter(rate);
let heard: Float32Array | null = null;
assert(segmenter.push(frame(100, 0.5)) === null, 'speech alone waits for a pause');
heard = segmenter.push(frame(700, 0));
assert(heard !== null, '700ms of quiet ends the phrase');
assert(heard!.length === 100 + 160, `speech plus a short tail, got ${heard!.length}`);
assert(Math.abs(heard![0] - 0.5) < 1e-6, 'phrase starts with the voice');
assert(heard![heard!.length - 1] === 0, 'phrase ends on the trimmed tail');

const gap = createUtteranceSegmenter(rate);
assert(gap.push(frame(100, 0.5)) === null, 'start');
assert(gap.push(frame(699, 0)) === null, 'a pause just under 700ms stays open');
const closed = gap.push(frame(1, 0));
assert(closed !== null && closed.length === 260, `closing sample emits 260, got ${closed?.length}`);

const held = createUtteranceSegmenter(rate);
assert(held.push(frame(80, 0.5)) === null, 'open phrase');
const flushed = held.flush();
assert(flushed !== null && flushed.length === 80, `flush keeps the open phrase, got ${flushed?.length}`);
assert(held.flush() === null, 'second flush is empty');

const preroll = createUtteranceSegmenter(rate);
for (let i = 0; i < 20; i++) preroll.push(frame(50, 0));
assert(preroll.push(frame(50, 0.5)) === null, 'voice after preroll');
const withLead = preroll.push(frame(700, 0));
assert(withLead !== null, 'preroll phrase emits');
assert(withLead!.length === 510, `preroll is kept and ancient silence is not, got ${withLead!.length}`);
assert(withLead![0] === 0, 'lead-in is silence');
assert(Math.abs(withLead![300] - 0.5) < 1e-6, 'voice sits just after the kept lead-in');

const split = createUtteranceSegmenter(rate);
const first = split.push(frame(12_000, 0.5));
assert(first !== null && first.length === 12_000, 'a 12s run is cut so the next sentence can start');
assert(split.push(frame(100, 0.5)) === null, 'the next phrase starts clean');
const second = split.flush();
assert(second !== null && second.length === 100, `second phrase is only the new audio, got ${second?.length}`);

let threw = false;
try { createUtteranceSegmenter(0); } catch { threw = true; }
assert(threw, 'a zero sample rate is refused');

console.log('voice-vad ok');
