import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Icon } from '../icons';
import {
  SPEECH_MODEL_LABEL,
  preloadTranscriber,
  subscribeModelProgress,
  transcribeBlob,
} from '../voiceTranscribe';

const MAX_MS = 60_000;
const IDLE_HINT = `Dictate in English, including Indian English. Runs in this browser (${SPEECH_MODEL_LABEL}). First use downloads about 40 MB.`;

type Phase = 'idle' | 'recording' | 'working';

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

function pickMime(): string {
  if (typeof MediaRecorder === 'undefined') return '';
  const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
  return types.find(t => MediaRecorder.isTypeSupported(t)) ?? '';
}

export function VoiceButton({ onText }: { onText: (spoken: string) => void }) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [percent, setPercent] = useState<number | null>(null);
  const [hint, setHint] = useState(IDLE_HINT);
  const phaseRef = useRef<Phase>('idle');
  const percentRef = useRef<number | null>(null);
  const alive = useRef(true);
  const session = useRef(0);
  const starting = useRef(false);
  const flushing = useRef(false);
  const recRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number | null>(null);
  const stopRef = useRef<() => void>(() => {});
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  function setPhaseBoth(next: Phase) {
    phaseRef.current = next;
    setPhase(next);
  }

  function setPercentBoth(next: number | null) {
    percentRef.current = next;
    setPercent(next);
  }

  stopRef.current = () => {
    if (timerRef.current != null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const rec = recRef.current;
    recRef.current = null;
    // Leave the tracks live until onstop copies the chunks. Stopping them in
    // this turn kills the encoder before it flushes, so the blob is empty.
    // A second stop (double-click, or unmount during that flush) must not
    // take the fallback and kill the tracks early.
    if (rec && rec.state !== 'inactive') {
      try {
        flushing.current = true;
        rec.stop();
        return;
      } catch {
        flushing.current = false;
      }
    }
    if (flushing.current) return;
    streamRef.current?.getTracks().forEach(track => track.stop());
    streamRef.current = null;
  };

  useEffect(() => subscribeModelProgress((info) => {
    if (!alive.current) return;
    if (info.status === 'progress_total') {
      const n = Math.round(info.progress);
      setPercentBoth(n);
      setHint(`Downloading speech model… ${n}%`);
    } else if (info.status === 'ready') {
      setPercentBoth(null);
      setHint(phaseRef.current === 'recording' ? 'Recording — click to stop' : 'Transcribing…');
    }
  }), []);

  useEffect(() => {
    // StrictMode runs this cleanup once on mount and reuses the same refs.
    // The setup must turn the guard back on, or the mic click always bails.
    alive.current = true;
    return () => {
      alive.current = false;
      session.current += 1;
      stopRef.current();
    };
  }, []);

  async function transcribe(blob: Blob, epoch: number) {
    const here = () => alive.current && epoch === session.current;
    if (here()) {
      setPhaseBoth('working');
      if (percentRef.current == null) setHint('Transcribing…');
    }
    try {
      const text = await transcribeBlob(blob);
      // The empty-chat mic unmounts once a session exists. Chat is still
      // mounted, so the words still belong in the box. State updates stay
      // behind `here()`.
      if (text) onTextRef.current(text);
      else if (here()) toast.warning("Didn't catch that.");
    } catch (err) {
      if (here()) toast.error(voiceError(err));
    } finally {
      if (!here()) return;
      setPhaseBoth('idle');
      setPercentBoth(null);
      setHint(IDLE_HINT);
    }
  }

  async function start() {
    if (starting.current || phaseRef.current !== 'idle') return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      toast.error('Voice input needs a secure page (https or localhost) and a microphone.');
      return;
    }
    starting.current = true;
    const epoch = session.current;
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
    } catch (err) {
      if (alive.current && epoch === session.current) toast.error(voiceError(err));
      return;
    } finally {
      starting.current = false;
    }
    if (!alive.current || epoch !== session.current || phaseRef.current !== 'idle') {
      stream.getTracks().forEach(track => track.stop());
      return;
    }
    streamRef.current = stream;
    preloadTranscriber();
    const mime = pickMime();
    let rec: MediaRecorder;
    try {
      rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    } catch (err) {
      stream.getTracks().forEach(track => track.stop());
      streamRef.current = null;
      if (alive.current) toast.error(voiceError(err));
      return;
    }
    chunksRef.current = [];
    rec.ondataavailable = (ev) => { if (ev.data.size) chunksRef.current.push(ev.data); };
    rec.onstop = () => {
      const blob = new Blob(chunksRef.current, { type: rec.mimeType || mime || 'audio/webm' });
      chunksRef.current = [];
      flushing.current = false;
      streamRef.current?.getTracks().forEach(track => track.stop());
      streamRef.current = null;
      void transcribe(blob, epoch);
    };
    recRef.current = rec;
    try {
      rec.start();
    } catch (err) {
      rec.onstop = null;
      recRef.current = null;
      stream.getTracks().forEach(track => track.stop());
      streamRef.current = null;
      if (alive.current) toast.error(voiceError(err));
      return;
    }
    setPhaseBoth('recording');
    setHint('Recording — click to stop');
    timerRef.current = window.setTimeout(() => {
      if (phaseRef.current !== 'recording' || epoch !== session.current) return;
      toast.warning('Recording stopped at 60 seconds.');
      setPhaseBoth('working');
      if (percentRef.current == null) setHint('Transcribing…');
      stopRef.current();
    }, MAX_MS);
  }

  function onClick() {
    if (phaseRef.current === 'working' || flushing.current) return;
    if (phaseRef.current === 'recording') {
      setPhaseBoth('working');
      if (percentRef.current == null) setHint('Transcribing…');
      stopRef.current();
      return;
    }
    void start();
  }

  const showSpinner = phase === 'working' && percent == null;
  return (
    <button
      type="button"
      className={phase === 'recording' ? 'voice-btn recording' : 'voice-btn'}
      onClick={onClick}
      disabled={phase === 'working'}
      title={hint}
      aria-label={phase === 'recording' ? 'Stop recording' : 'Dictate'}
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
