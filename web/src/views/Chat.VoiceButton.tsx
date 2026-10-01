import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Icon } from '../icons';
import {
  SPEECH_MODEL_LABEL,
  SPEECH_RMS,
  createUtteranceSegmenter,
  preloadTranscriber,
  rms,
  subscribeModelProgress,
  transcribeSamples,
} from '../voiceTranscribe';
import { correctSpoken } from '../voiceCorrect';
import type { SpokenAnchor } from '../voicePhrase';

const IDLE_HINT = `Click and talk. A short pause writes the words into the box, then the chat model tidies the sentence (${SPEECH_MODEL_LABEL}, on this device). Click again when you are done — nothing is sent until you press Run. First use downloads about 40 MB.`;
const LISTENING_HINT = 'Listening. A short pause writes the words. Click to finish.';
const QUIET_AFTER_SPEECH_MS = 8_000;
const QUIET_IF_SILENT_MS = 20_000;
/** No frames for this long means the audio graph stopped. */
const GRAPH_DEAD_MS = 3_000;
/**
 * ScriptProcessor fires every 2048 samples (128ms at 16 kHz). After a stall,
 * wait longer than one buffer before treating that gap as a dead graph.
 */
const GRAPH_CONFIRM_MS = 300;
/**
 * Stop waits this long before closing the phrase. A 0ms timer can run before
 * worklet messages already sitting in the port; those messages are a backlog
 * of a few milliseconds, so 50ms lands after them.
 */
const DRAIN_MS = 50;

type Phase = 'idle' | 'recording' | 'working';

// Captures on the audio thread so a Whisper pass on the main thread does not
// drop the next sentence. ScriptProcessor is the fallback for browsers
// without AudioWorklet; it shares the main thread with the model.
const PCM_WORKLET = `
class AiforgePcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channels = inputs[0];
    const first = channels && channels[0];
    if (!first || !first.length) return true;
    const out = new Float32Array(first.length);
    const count = channels.length;
    for (let c = 0; c < count; c++) {
      const ch = channels[c];
      if (!ch) continue;
      for (let i = 0; i < first.length; i++) out[i] += ch[i] / count;
    }
    this.port.postMessage(out);
    return true;
  }
}
registerProcessor('aiforge-pcm-capture', AiforgePcmCapture);
`;

function voiceError(err: unknown): string {
  const name = err instanceof Error ? err.name : '';
  const message = err instanceof Error ? err.message : String(err);
  if (name === 'NotAllowedError' || name === 'PermissionDeniedError') return 'Microphone permission was denied.';
  if (name === 'NotFoundError') return 'No microphone was found.';
  if (name === 'NotReadableError') return 'The microphone is in use by another app.';
  if (/jsdelivr|onnxruntime|\.wasm/i.test(message)) {
    return "Couldn't load the speech runtime from this app. Reload and try the mic again.";
  }
  if (/failed to fetch|network|huggingface|offline/i.test(message)) {
    return "Couldn't download the speech model. This browser needs to reach huggingface.co the first time.";
  }
  return `Voice input failed: ${message}`;
}

function disconnectQuietly(node: AudioNode): void {
  try { node.disconnect(); } catch { /* already disconnected */ }
}

function pcmFromWorklet(data: unknown): Float32Array | null {
  if (data instanceof Float32Array) return data;
  // postMessage usually re-wraps the array in this realm. If it does not,
  // instanceof fails and the constructor name is still Float32Array.
  if (typeof data !== 'object' || data === null) return null;
  const foreign = data as { constructor?: { name?: string }; length?: number };
  if (foreign.constructor?.name !== 'Float32Array' || typeof foreign.length !== 'number' || foreign.length <= 0) return null;
  try {
    const copy = new Float32Array(foreign.length);
    copy.set(data as Float32Array);
    return copy;
  } catch {
    return null;
  }
}

function connectScriptProcessor(ctx: AudioContext, source: AudioNode, node: AudioNode): () => void {
  // Chrome does not run a ScriptProcessor unless the graph reaches
  // AudioContext.destination. A media-stream sink does not count. Gain 0
  // keeps the microphone out of the speakers.
  const mute = ctx.createGain();
  mute.gain.value = 0;
  source.connect(node);
  node.connect(mute);
  mute.connect(ctx.destination);
  return () => {
    disconnectQuietly(source);
    disconnectQuietly(node);
    disconnectQuietly(mute);
  };
}

function connectSink(ctx: AudioContext, source: AudioNode, node: AudioNode): () => void {
  // A media-stream sink keeps the processor pulling without playing the mic.
  const sink = ctx.createMediaStreamDestination();
  source.connect(node);
  node.connect(sink);
  return () => {
    disconnectQuietly(source);
    disconnectQuietly(node);
    disconnectQuietly(sink);
  };
}

async function attachCapture(
  ctx: AudioContext,
  stream: MediaStream,
  onFrame: (frame: Float32Array) => void,
): Promise<() => void> {
  const source = ctx.createMediaStreamSource(stream);

  if (ctx.audioWorklet) {
    const url = URL.createObjectURL(new Blob([PCM_WORKLET], { type: 'application/javascript' }));
    try {
      await ctx.audioWorklet.addModule(url);
      const node = new AudioWorkletNode(ctx, 'aiforge-pcm-capture', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        channelCount: 1,
        channelCountMode: 'explicit',
      });
      node.port.onmessage = (ev: MessageEvent) => {
        const frame = pcmFromWorklet(ev.data);
        if (frame) onFrame(frame);
      };
      // Leave port.onmessage in place. Stop unplugs the graph, then a
      // queued frame still has to reach the segmenter before the phrase
      // is closed. captureOpenRef drops anything that arrives later.
      return connectSink(ctx, source, node);
    } catch {
      disconnectQuietly(source);
      // Blob worklets are blocked in a few browsers. Fall through.
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  const legacy = ctx as AudioContext & {
    createScriptProcessor?: (size: number, inputs: number, outputs: number) => ScriptProcessorNode;
  };
  if (typeof legacy.createScriptProcessor !== 'function') {
    throw new Error('This browser cannot capture microphone audio.');
  }
  const node = legacy.createScriptProcessor(2048, 1, 1);
  node.onaudioprocess = (ev) => {
    onFrame(new Float32Array(ev.inputBuffer.getChannelData(0)));
  };
  return connectScriptProcessor(ctx, source, node);
}

export function VoiceButton({ onText, onCorrect }: {
  onText: (spoken: string) => SpokenAnchor;
  onCorrect?: (anchor: SpokenAnchor, corrected: string) => void;
}) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [percent, setPercent] = useState<number | null>(null);
  const [hint, setHint] = useState(IDLE_HINT);
  const phaseRef = useRef<Phase>('idle');
  const percentRef = useRef<number | null>(null);
  const alive = useRef(true);
  const session = useRef(0);
  const starting = useRef(false);
  const streamRef = useRef<MediaStream | null>(null);
  const audioRef = useRef<AudioContext | null>(null);
  const detachRef = useRef<(() => void) | null>(null);
  const captureOpenRef = useRef(false);
  const queueRef = useRef(Promise.resolve());
  const wroteRef = useRef(false);
  const heardSpeechRef = useRef(false);
  const silenceSamplesRef = useRef(0);
  const framesSeenRef = useRef(false);
  const lastFrameAtRef = useRef(0);
  const quietArmedRef = useRef(false);
  const startedAtRef = useRef(0);
  const watchRef = useRef<number | null>(null);
  const onTextRef = useRef(onText);
  const onCorrectRef = useRef(onCorrect);
  onTextRef.current = onText;
  onCorrectRef.current = onCorrect;

  function setPhaseBoth(next: Phase) {
    phaseRef.current = next;
    setPhase(next);
  }

  function setPercentBoth(next: number | null) {
    percentRef.current = next;
    setPercent(next);
  }

  function releaseMic() {
    captureOpenRef.current = false;
    if (watchRef.current != null) {
      window.clearInterval(watchRef.current);
      watchRef.current = null;
    }
    const detach = detachRef.current;
    detachRef.current = null;
    detach?.();
    const ctx = audioRef.current;
    audioRef.current = null;
    if (ctx && ctx.state !== 'closed') void ctx.close();
    streamRef.current?.getTracks().forEach(track => track.stop());
    streamRef.current = null;
  }

  const releaseMicRef = useRef(releaseMic);
  releaseMicRef.current = releaseMic;
  // Replaced with the live session's finish() once listening starts.
  // Assigning it during render would wipe that closure on the next setState.
  const stopRef = useRef<() => void>(() => { releaseMicRef.current(); });

  useEffect(() => subscribeModelProgress((info) => {
    if (!alive.current) return;
    if (info.status === 'progress_total') {
      const n = Math.round(info.progress);
      setPercentBoth(n);
      setHint(`Downloading speech model… ${n}%`);
    } else if (info.status === 'ready') {
      setPercentBoth(null);
      if (phaseRef.current === 'recording') setHint(LISTENING_HINT);
      else if (phaseRef.current === 'working') setHint('Writing what you said…');
    }
  }), []);

  useEffect(() => {
    // StrictMode runs this cleanup once on mount and reuses the same refs.
    // The setup must turn the guard back on, or the mic click always bails.
    alive.current = true;
    return () => {
      alive.current = false;
      // Finish while this session id still matches, then invalidate it so a
      // late start() or error toast cannot land on the next mount.
      stopRef.current();
      session.current += 1;
    };
  }, []);

  async function tidy(anchor: SpokenAnchor) {
    if (anchor.start < 0) return;
    const fixed = await correctSpoken(anchor.raw);
    if (!fixed || fixed === anchor.raw) return;
    onCorrectRef.current?.(anchor, fixed);
  }

  function enqueue(samples: Float32Array, sampleRate: number, epoch: number) {
    queueRef.current = queueRef.current.then(async () => {
      const text = await transcribeSamples(samples, sampleRate);
      if (!text) return;
      wroteRef.current = true;
      // The empty-chat mic unmounts once a session exists. Chat is still
      // mounted, so the words still belong in the box. The tidy runs beside
      // the next phrase so a model call does not hold up the following sentence.
      void tidy(onTextRef.current(text));
    }).catch((err: unknown) => {
      if (alive.current && epoch === session.current) toast.error(voiceError(err));
    });
  }

  type Segmenter = ReturnType<typeof createUtteranceSegmenter>;

  function quietLongEnough(sampleRate: number): boolean {
    const silenceMs = silenceSamplesRef.current / sampleRate * 1000;
    const limit = heardSpeechRef.current ? QUIET_AFTER_SPEECH_MS : QUIET_IF_SILENT_MS;
    return silenceMs >= limit;
  }

  async function confirmGraphDead(epoch: number, segmenter: Segmenter, sampleRate: number) {
    if (quietArmedRef.current || phaseRef.current !== 'recording' || epoch !== session.current) return;
    const mark = lastFrameAtRef.current;
    quietArmedRef.current = true;
    await new Promise<void>(resolve => { window.setTimeout(resolve, GRAPH_CONFIRM_MS); });
    quietArmedRef.current = false;
    if (phaseRef.current !== 'recording' || epoch !== session.current) return;
    // A Whisper stall leaves this stamp old while frames are queued. If any
    // arrived during the wait, the graph is alive and the sample counter decides.
    if (lastFrameAtRef.current !== mark) return;
    if (performance.now() - mark < GRAPH_DEAD_MS) return;
    void finish(epoch, segmenter, sampleRate);
  }

  async function finish(epoch: number, segmenter: Segmenter, sampleRate: number) {
    if (phaseRef.current !== 'recording' || epoch !== session.current) return;
    // Synchronous, so a second click or the quiet-timer cannot re-enter
    // before React re-renders. Skip setState when the button is already gone.
    phaseRef.current = 'working';
    // Unmount during the drain must not releaseMic yet: that would close the
    // capture flag and drop worklet frames that have not been segmented.
    stopRef.current = () => { /* finish already owns teardown */ };
    const here = () => alive.current && epoch === session.current;
    if (here()) {
      setPhase('working');
      if (percentRef.current == null) setHint('Writing what you said…');
    }
    if (watchRef.current != null) {
      window.clearInterval(watchRef.current);
      watchRef.current = null;
    }
    // Unplug the mic graph first. Audio already posted from the worklet is
    // still delivered on the next turn, then the open phrase is transcribed.
    // capture stays open across that turn: unmount bumps the session id
    // before this await resumes, and those samples still belong here.
    const detach = detachRef.current;
    detachRef.current = null;
    detach?.();
    let tail: Float32Array | null = null;
    try {
      await new Promise<void>(resolve => { window.setTimeout(resolve, DRAIN_MS); });
    } finally {
      captureOpenRef.current = false;
      tail = segmenter.flush();
      releaseMic();
      stopRef.current = () => { releaseMicRef.current(); };
    }
    if (tail) enqueue(tail, sampleRate, epoch);
    try {
      await queueRef.current;
    } finally {
      if (!here()) return;
      if (!wroteRef.current) toast.warning("Didn't catch that.");
      setPhaseBoth('idle');
      setPercentBoth(null);
      setHint(IDLE_HINT);
    }
  }

  function dropStream(streamPromise: Promise<MediaStream>) {
    void streamPromise.then(stream => {
      stream.getTracks().forEach(track => track.stop());
    }).catch(() => { /* getUserMedia already reported the failure */ });
  }

  async function start(ctx: AudioContext, resumed: Promise<void>, streamPromise: Promise<MediaStream>) {
    if (starting.current || phaseRef.current !== 'idle') {
      void ctx.close();
      dropStream(streamPromise);
      return;
    }
    starting.current = true;
    const epoch = session.current;
    audioRef.current = ctx;
    try {
      const stream = await streamPromise;
      await resumed;
      if (ctx.state !== 'running') throw new Error('The browser blocked microphone audio.');
      if (!alive.current || epoch !== session.current || phaseRef.current !== 'idle') {
        stream.getTracks().forEach(track => track.stop());
        releaseMic();
        return;
      }
      streamRef.current = stream;
      preloadTranscriber();
      const segmenter = createUtteranceSegmenter(ctx.sampleRate);
      const detach = await attachCapture(ctx, stream, (frame) => {
        // Session id is not checked. Unmount increments it before the stop
        // drain runs, and a frame already in the worklet port still belongs
        // to this phrase.
        if (!captureOpenRef.current) return;
        framesSeenRef.current = true;
        lastFrameAtRef.current = performance.now();
        if (rms(frame) >= SPEECH_RMS) {
          heardSpeechRef.current = true;
          silenceSamplesRef.current = 0;
        } else {
          silenceSamplesRef.current += frame.length;
        }
        const phrase = segmenter.push(frame);
        if (phrase) enqueue(phrase, ctx.sampleRate, epoch);
        // Decide from samples, in audio order. A timer can see a stale
        // silence count while a backlog of speech is still queued.
        if (phaseRef.current === 'recording' && quietLongEnough(ctx.sampleRate)) {
          void finish(epoch, segmenter, ctx.sampleRate);
        }
      });
      if (!alive.current || epoch !== session.current || phaseRef.current !== 'idle') {
        detach();
        releaseMic();
        return;
      }
      captureOpenRef.current = true;
      detachRef.current = detach;
      wroteRef.current = false;
      heardSpeechRef.current = false;
      silenceSamplesRef.current = 0;
      framesSeenRef.current = false;
      lastFrameAtRef.current = 0;
      quietArmedRef.current = false;
      startedAtRef.current = performance.now();
      stopRef.current = () => { void finish(epoch, segmenter, ctx.sampleRate); };
      setPhaseBoth('recording');
      setHint(LISTENING_HINT);
      watchRef.current = window.setInterval(() => {
        if (phaseRef.current !== 'recording' || epoch !== session.current) return;
        const now = performance.now();
        if (!framesSeenRef.current) {
          if (now - startedAtRef.current >= QUIET_IF_SILENT_MS) void finish(epoch, segmenter, ctx.sampleRate);
          return;
        }
        if (now - lastFrameAtRef.current >= GRAPH_DEAD_MS) {
          void confirmGraphDead(epoch, segmenter, ctx.sampleRate);
        }
      }, 250);
    } catch (err) {
      releaseMic();
      dropStream(streamPromise);
      if (alive.current && epoch === session.current) toast.error(voiceError(err));
    } finally {
      starting.current = false;
    }
  }

  function onClick() {
    if (starting.current || phaseRef.current === 'working') return;
    if (phaseRef.current === 'recording') {
      stopRef.current();
      return;
    }
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || typeof AudioContext === 'undefined') {
      toast.error('Voice input needs a secure page (https or localhost) and a microphone.');
      return;
    }
    // Both calls have to start inside the click. After the permission await,
    // the browser may no longer treat the page as activated, and a context
    // created then stays suspended.
    const ctx = new AudioContext();
    const resumed = ctx.resume();
    const streamPromise = navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        channelCount: { ideal: 1 },
      },
    });
    void start(ctx, resumed, streamPromise);
  }

  const showSpinner = phase === 'working' && percent == null;
  return (
    <button
      type="button"
      className={phase === 'recording' ? 'voice-btn recording' : 'voice-btn'}
      onClick={onClick}
      disabled={phase === 'working'}
      title={hint}
      aria-label={phase === 'recording' ? 'Stop dictation' : 'Dictate'}
      aria-pressed={phase === 'recording'}
      style={{ whiteSpace: 'nowrap', display: 'inline-flex', alignItems: 'center' }}
    >
      {showSpinner
        ? <span className="af-spin"><Icon.Refresh size={15} /></span>
        : <Icon.Mic size={15} />}
      {percent != null && <span className="xs">{percent}%</span>}
    </button>
  );
}
