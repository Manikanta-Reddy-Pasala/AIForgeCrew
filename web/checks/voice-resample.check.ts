import { resampleLinear, rms } from '../src/voiceTranscribe.ts';

function assert(cond: boolean, msg: string): void {
  if (!cond) throw new Error(msg);
}

const same = new Float32Array([0.25, -0.5, 0.75]);
const identity = resampleLinear(same, 16_000, 16_000);
assert(identity === same, 'same rate should return the same array');

const flat = new Float32Array(4800);
flat.fill(0.5);
const down = resampleLinear(flat, 48_000, 16_000);
assert(down.length === 1600, `48k→16k length ${down.length}`);
for (let i = 0; i < down.length; i++) {
  if (Math.abs(down[i] - 0.5) > 1e-6) throw new Error(`sample ${i} drifted to ${down[i]}`);
}

const quiet = new Float32Array(1600);
assert(rms(quiet) === 0, 'silence rms');
const loud = new Float32Array(4);
loud.fill(0.5);
assert(Math.abs(rms(loud) - 0.5) < 1e-6, `rms ${rms(loud)}`);

const empty = resampleLinear(new Float32Array(0), 48_000, 16_000);
assert(empty.length === 0, 'empty stays empty');

console.log('voice-resample ok');
