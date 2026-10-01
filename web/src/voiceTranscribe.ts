// In-browser speech-to-text for the chat composer.
//
// Xenova/whisper-tiny.en (q8, WASM) is the smallest English Whisper that still
// handles accented speech, including Indian English. Moonshine tiny is smaller
// but weaker on accents. The multilingual tiny model spends capacity on other
// languages. Weights stay out of the app bundle: the first mic click downloads
// them from Hugging Face (~40 MB) and the browser Cache API keeps them.

import type { ProgressInfo } from '@huggingface/transformers';

export const SPEECH_MODEL = 'Xenova/whisper-tiny.en';
export const SPEECH_MODEL_LABEL = 'Whisper tiny English';

const TARGET_RATE = 16_000;
const MAX_SECONDS = 60;
const MIN_SECONDS = 0.3;
/** Below this RMS the clip is silence; Whisper would hallucinate a phrase. */
const SILENCE_RMS = 0.004;

const listeners = new Set<(info: ProgressInfo) => void>();

export function subscribeModelProgress(cb: (info: ProgressInfo) => void): () => void {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
}

function emit(info: ProgressInfo): void {
  for (const cb of listeners) cb(info);
}

type AsrFn = (
  audio: Float32Array,
  options?: { chunk_length_s?: number; stride_length_s?: number },
) => Promise<{ text: string } | Array<{ text: string }>>;

let pending: Promise<AsrFn> | null = null;

function speechRuntimeUrl(file: string): string {
  const base = import.meta.env.BASE_URL || '/';
  return `${base}speech/${file}`;
}

async function createTranscriber(): Promise<AsrFn> {
  const { pipeline, env } = await import('@huggingface/transformers');
  env.allowLocalModels = false;
  env.useBrowserCache = true;
  // The library would fetch the WASM runtime from
  // cdn.jsdelivr.net/npm/onnxruntime-web@<dev-version>/, which fails with
  // ERR_CONNECTION_CLOSED on networks that cannot reach that host. The same
  // files are served by this app under /ui/speech/.
  // One WASM thread: threaded WASM needs cross-origin isolation, which this
  // page does not set, and tiny English is fast enough on a single thread.
  const wasm = env.backends.onnx.wasm;
  if (wasm) {
    wasm.numThreads = 1;
    wasm.wasmPaths = {
      mjs: speechRuntimeUrl('ort-wasm-simd-threaded.asyncify.mjs'),
      wasm: speechRuntimeUrl('ort-wasm-simd-threaded.asyncify.wasm'),
    };
  }
  const asr = await pipeline('automatic-speech-recognition', SPEECH_MODEL, {
    dtype: 'q8',
    device: 'wasm',
    progress_callback: emit,
  });
  return async (audio, options) => {
    const result = await asr(audio, options);
    return result as { text: string } | Array<{ text: string }>;
  };
}

export function loadTranscriber(): Promise<AsrFn> {
  if (!pending) {
    pending = createTranscriber().catch((err: unknown) => {
      pending = null;
      throw err;
    });
  }
  return pending;
}

/** Start the download while the user is still talking. Failures surface on transcribe. */
export function preloadTranscriber(): void {
  void loadTranscriber().catch(() => { /* transcribeBlob reports this */ });
}

/** Linear resample. `fromRate === toRate` returns the same array. */
export function resampleLinear(input: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (input.length === 0 || fromRate === toRate) return input;
  if (fromRate <= 0 || toRate <= 0) throw new Error('Sample rate must be positive.');
  const outLen = Math.max(1, Math.round(input.length * toRate / fromRate));
  const out = new Float32Array(outLen);
  const scale = (input.length - 1) / Math.max(outLen - 1, 1);
  for (let i = 0; i < outLen; i++) {
    const pos = i * scale;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = pos - i0;
    out[i] = input[i0] * (1 - frac) + input[i1] * frac;
  }
  return out;
}

export function rms(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

function mixToMono(buffer: AudioBuffer): Float32Array {
  const n = buffer.length;
  const channels = buffer.numberOfChannels;
  if (channels === 1) return new Float32Array(buffer.getChannelData(0));
  const out = new Float32Array(n);
  for (let c = 0; c < channels; c++) {
    const data = buffer.getChannelData(c);
    for (let i = 0; i < n; i++) out[i] += data[i] / channels;
  }
  return out;
}

async function blobTo16k(blob: Blob): Promise<Float32Array> {
  if (typeof AudioContext === 'undefined') {
    throw new Error('This browser cannot decode microphone audio.');
  }
  const copy = (await blob.arrayBuffer()).slice(0);
  const ctx = new AudioContext();
  try {
    const decoded = await ctx.decodeAudioData(copy);
    const mono = mixToMono(decoded);
    const samples = resampleLinear(mono, decoded.sampleRate, TARGET_RATE);
    const max = TARGET_RATE * MAX_SECONDS;
    return samples.length > max ? samples.subarray(0, max) : samples;
  } finally {
    await ctx.close();
  }
}

// One WASM session. The empty-chat button can still be inside asr() after it
// unmounts, and the session button must not start a second call on it.
let transcribeGate: Promise<void> = Promise.resolve();

async function transcribePcmNow(samples: Float32Array): Promise<string> {
  const transcriber = await loadTranscriber();
  const seconds = samples.length / TARGET_RATE;
  // Whisper's window is 30s. Shorter clips are one pass; longer ones overlap.
  const options = seconds > 28 ? { chunk_length_s: 30, stride_length_s: 5 } : undefined;
  const result = await transcriber(samples, options);
  const text = Array.isArray(result) ? result.map(part => part.text).join(' ') : result.text;
  return text.replace(/\s+/g, ' ').trim();
}

function transcribePcm(samples: Float32Array): Promise<string> {
  if (samples.length < TARGET_RATE * MIN_SECONDS || rms(samples) < SILENCE_RMS) return Promise.resolve('');
  const run = transcribeGate.then(() => transcribePcmNow(samples));
  transcribeGate = run.then(() => undefined, () => undefined);
  return run;
}

/** Returns '' when the clip is too short, silent, or not decodable. */
export async function transcribeBlob(blob: Blob): Promise<string> {
  if (blob.size < 500) return '';
  let samples: Float32Array;
  try {
    samples = await blobTo16k(blob);
  } catch (err) {
    // A tap on the mic produces a tiny or empty container. A real recording
    // that fails to decode should still surface.
    if (blob.size < 8_000) return '';
    throw err;
  }
  return transcribePcm(samples);
}

/** `samples` are mono PCM at `sampleRate`. Resampled to 16 kHz for Whisper. */
export async function transcribeSamples(samples: Float32Array, sampleRate: number): Promise<string> {
  const audio = resampleLinear(samples, sampleRate, TARGET_RATE);
  const max = TARGET_RATE * MAX_SECONDS;
  return transcribePcm(audio.length > max ? audio.subarray(0, max) : audio);
}

/** A frame this loud counts as speech. Quiet rooms sit well under it. */
export const SPEECH_RMS = 0.01;
const PAUSE_MS = 700;
const PREROLL_MS = 280;
const TAIL_MS = 160;
const MAX_UTTERANCE_MS = 12_000;

export type UtteranceSegmenter = {
  /** Samples to transcribe when a phrase just ended, otherwise null. */
  push(frame: Float32Array): Float32Array | null;
  /** Any phrase still open when listening stops. */
  flush(): Float32Array | null;
};

function joinFrames(frames: Float32Array[], length: number): Float32Array {
  const out = new Float32Array(length);
  let offset = 0;
  for (const frame of frames) {
    out.set(frame, offset);
    offset += frame.length;
  }
  return out;
}

/**
 * Splits a live mic into phrases. A short pause ends the phrase so it can be
 * transcribed while the next one is still being captured. A little audio from
 * before the voice starts is kept, so the first consonant is not clipped.
 */
export function createUtteranceSegmenter(sampleRate: number): UtteranceSegmenter {
  if (sampleRate <= 0) throw new Error('Sample rate must be positive.');
  const prerollMax = Math.round(sampleRate * PREROLL_MS / 1000);
  const tail = Math.round(sampleRate * TAIL_MS / 1000);
  const pauseSamples = Math.round(sampleRate * PAUSE_MS / 1000);
  const maxSamples = Math.round(sampleRate * MAX_UTTERANCE_MS / 1000);

  let preroll: Float32Array[] = [];
  let prerollLen = 0;
  let speech: Float32Array[] = [];
  let speechLen = 0;
  let silenceRun = 0;
  let speaking = false;

  function emit(): Float32Array | null {
    if (!speaking || speechLen === 0) {
      speaking = false;
      speech = [];
      speechLen = 0;
      silenceRun = 0;
      return null;
    }
    const drop = Math.max(0, silenceRun - tail);
    const full = joinFrames(speech, speechLen);
    const samples = drop > 0 && drop < full.length ? full.subarray(0, full.length - drop) : full;
    speaking = false;
    speech = [];
    speechLen = 0;
    silenceRun = 0;
    preroll = [];
    prerollLen = 0;
    return samples.length > 0 ? samples : null;
  }

  return {
    push(frame) {
      const voiced = rms(frame) >= SPEECH_RMS;
      if (!speaking) {
        if (!voiced) {
          preroll.push(frame);
          prerollLen += frame.length;
          while (preroll.length > 1 && prerollLen - preroll[0].length >= prerollMax) {
            prerollLen -= preroll.shift()!.length;
          }
          return null;
        }
        speech = preroll;
        speechLen = prerollLen;
        preroll = [];
        prerollLen = 0;
        speaking = true;
        silenceRun = 0;
      }
      speech.push(frame);
      speechLen += frame.length;
      silenceRun = voiced ? 0 : silenceRun + frame.length;
      if (silenceRun >= pauseSamples || speechLen >= maxSamples) return emit();
      return null;
    },
    flush() {
      return emit();
    },
  };
}
