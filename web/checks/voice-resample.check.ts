import { cleanTranscript, resampleLinear, rms } from '../src/voiceTranscribe.ts';

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

// Downsampling must not fold high frequencies into the speech band. A 12 kHz
// tone sampled at 48 kHz is above what 16 kHz can carry: it has to come out
// (nearly) silent, not as a loud 4 kHz alias.
const tone = new Float32Array(4800);
for (let i = 0; i < tone.length; i++) tone[i] = Math.sin(2 * Math.PI * 12_000 * i / 48_000);
const folded = rms(resampleLinear(tone, 48_000, 16_000));
assert(folded < 0.4, `12 kHz tone leaked through at rms ${folded}`);
// …while a 1 kHz tone, well inside the band, survives.
const voice = new Float32Array(4800);
for (let i = 0; i < voice.length; i++) voice[i] = Math.sin(2 * Math.PI * 1_000 * i / 48_000);
const kept = rms(resampleLinear(voice, 48_000, 16_000));
assert(kept > 0.6, `1 kHz tone lost: rms ${kept}`);

// Upsampling still interpolates between neighbours.
const up = resampleLinear(new Float32Array([0, 1]), 8_000, 16_000);
assert(up.length === 4 && up[0] === 0 && Math.abs(up[3] - 1) < 1e-6, 'upsample endpoints');

assert(cleanTranscript(' [BLANK_AUDIO] ') === '', 'a blank-audio label is not words');
assert(cleanTranscript('(music)') === '', 'a sound label is not words');
assert(cleanTranscript('Save the file. [ Silence ]') === 'Save the file.', 'label after a sentence');
assert(cleanTranscript('add *coughs* the button') === 'add the button', 'label inside a sentence');
assert(cleanTranscript('call foo(bar) now') === 'call foo now', 'short parenthetical removed');
assert(cleanTranscript('  two   spaces ') === 'two spaces', 'whitespace collapsed');

console.log('voice-resample ok');
