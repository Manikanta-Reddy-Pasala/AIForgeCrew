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
  if (samples.length < TARGET_RATE * MIN_SECONDS || rms(samples) < SILENCE_RMS) return '';
  const transcriber = await loadTranscriber();
  const seconds = samples.length / TARGET_RATE;
  // Whisper's window is 30s. Shorter clips are one pass; longer ones overlap.
  const options = seconds > 28 ? { chunk_length_s: 30, stride_length_s: 5 } : undefined;
  const result = await transcriber(samples, options);
  const text = Array.isArray(result) ? result.map(part => part.text).join(' ') : result.text;
  return text.replace(/\s+/g, ' ').trim();
}
