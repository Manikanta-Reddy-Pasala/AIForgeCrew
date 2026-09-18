// The live-turn reducer: applies one chat SSE event to the in-progress
// assistant turn. Pure, so it can be exercised without the view.
import type { AgentStep, LiveTurn, SubtaskItem } from './Chat.model';

// A 'tool' event: flip the matching pending row to its real result (matched on
// call_id), or append when there's no pending row (hook-blocked/rejected path).
function reduceToolEvent(prev: LiveTurn, evt: any): LiveTurn {
  const idx = evt.call_id !== undefined
    ? prev.steps.findIndex(s => s.kind === 'tool' && s.pending && s.call_id === evt.call_id)
    : -1;
  if (idx !== -1) {
    const steps = [...prev.steps];
    steps[idx] = { kind: 'tool' as const, name: evt.name, args: evt.args || {}, result: evt.result || {}, role: evt.role, call_id: evt.call_id };
    return { ...prev, steps };
  }
  return { ...prev, steps: [...prev.steps, { kind: 'tool' as const, name: evt.name, args: evt.args || {}, result: evt.result || {}, role: evt.role }] };
}

// A 'message' event: a supplementary report is an extra step; the primary
// message replaces the answer text and ends streaming.
function reduceMessageEvent(prev: LiveTurn, evt: any, onAwaiting: () => void): LiveTurn {
  if (evt.supplementary) {
    return { ...prev, steps: [...prev.steps, { kind: 'message' as const, text: evt.text, role: evt.role }] };
  }
  if (evt.awaiting_input) onAwaiting();
  return { ...prev, text: evt.text, streaming: false, awaiting: !!evt.awaiting_input };
}

// Apply a subtask status update by slug (named so it stays off reduceTurn's
// complexity budget).
function withSubtaskStatus(subtasks: SubtaskItem[], slug: string, status: string): SubtaskItem[] {
  return subtasks.map(s => s.slug === slug ? { ...s, status } : s);
}

// Append a plain step (thought/tool_start/changes) — the simple, guard-free
// event types, resolved by a small lookup so reduceTurn stays flat.
function appendStepFor(prev: LiveTurn, evt: any): LiveTurn | null {
  if (evt.type === 'thought') {
    return { ...prev, steps: [...prev.steps, { kind: 'thought' as const, text: evt.text, role: evt.role }] };
  }
  if (evt.type === 'tool_start') {
    // Live "it's running" row — flipped to the real result by the matching
    // 'tool' event (matched on call_id) instead of showing nothing while a
    // slow bash/test/build runs.
    return { ...prev, steps: [...prev.steps, { kind: 'tool' as const, name: evt.name, args: evt.args || {}, result: {}, role: evt.role, pending: true, call_id: evt.call_id }] };
  }
  if (evt.type === 'changes') {
    return { ...prev, steps: [...prev.steps, { kind: 'changes' as const, files: evt.files || [], summary: evt.summary || { files: (evt.files || []).length, additions: 0, deletions: 0 } }] };
  }
  if (evt.type === 'error') {
    return { ...prev, text: evt.text, steps: [...prev.steps, { kind: 'error' as const, text: evt.text }], streaming: false };
  }
  if (evt.type === 'done') {
    return { ...prev, streaming: false };
  }
  return null;
}

// A 'delta' event: the reply as the model writes it. "answer" text streams into
// the bubble; "draft" (a tool step being written) and "thinking" (reasoning)
// keep only a short muted tail; "reset" starts a new model call afresh.
const DRAFT_TAIL = 240;
function reduceDelta(prev: LiveTurn, evt: any): LiveTurn {
  switch (evt.phase) {
    case 'reset': return { ...prev, streamText: '', draft: '' };
    case 'answer': return { ...prev, streamText: (prev.streamText ?? '') + (evt.text ?? '') };
    case 'draft':
    case 'thinking': return { ...prev, draft: ((prev.draft ?? '') + (evt.text ?? '')).slice(-DRAFT_TAIL) };
    default: return prev;
  }
}

// Events that settle a model call: its streamed text is replaced by the real
// step / answer they carry.
const SETTLES_STREAM = new Set(['thought', 'tool_start', 'tool', 'message', 'error', 'done']);

const squash = (s: string) => s.replace(/\s+/g, ' ').trim().toLowerCase();

// Two texts are the same reply when one holds the other: the final message is
// the streamed text minus a leading label line or a "…FINAL:" prefix, and a
// partial kept at a retry is the start of the full answer.
export function similarText(a: string, b: string): boolean {
  const x = squash(a), y = squash(b);
  if (!x || !y) return false;
  const [short, long] = x.length <= y.length ? [x, y] : [y, x];
  return long.includes(short) && short.length >= Math.min(24, long.length);
}

// Text-protocol lines ("ACTION: …", "ARGS_JSON: {…}") that streamed as plain
// answer text are the tool call, which gets its own row — not reply text.
function withoutProtocol(text: string): string {
  const cut = text.search(/^[ \t]*(ACTION|ARGS_JSON)[ \t]*:/m);
  return (cut >= 0 ? text.slice(0, cut) : text).trim();
}

// Only the model's own thought, or a message carrying the same text, holds
// what was streaming. Any other settling event (a system gate note such as
// "checking all 3 parts…", a tool call, an error, a backend-written "stopped"
// message) does not — wiping the stream there made a reply the user was
// reading vanish mid-turn. Keep it as a step instead.
function keepStreamed(prev: LiveTurn, evt: any): LiveTurn {
  const text = withoutProtocol(prev.streamText ?? '');
  const carriesText = (evt.type === 'message' && !evt.supplementary && similarText(text, evt.text ?? ''))
    || (evt.type === 'thought' && evt.role !== 'system');
  if (!text || carriesText) return prev;
  return { ...prev, steps: [...prev.steps, { kind: 'thought' as const, text, streamed: true }] };
}

// The same text arriving for real (the model thought, or the final message)
// replaces a kept copy rather than showing it twice.
function dropKeptCopy(steps: AgentStep[], text: string | undefined): AgentStep[] {
  if (!text) return steps;
  return steps.filter(s => !(s.kind === 'thought' && s.streamed && similarText(s.text, text)));
}

// The live-turn "step" reducer (subtasks/thought/tool/changes/message/…).
export function reduceTurn(prev: LiveTurn | null, evt: any, onAwaiting: () => void): LiveTurn | null {
  if (!prev) return prev;
  if (evt.type === 'delta') return reduceDelta(prev, evt);
  const kept = SETTLES_STREAM.has(evt.type) ? keepStreamed(prev, evt) : prev;
  const deduped = (evt.type === 'message' || evt.type === 'thought') && !evt.supplementary
    ? { ...kept, steps: dropKeptCopy(kept.steps, evt.text) }
    : kept;
  const next = reduceStep(deduped, evt, onAwaiting);
  return next && SETTLES_STREAM.has(evt.type) ? { ...next, streamText: '', draft: '' } : next;
}

function reduceStep(prev: LiveTurn, evt: any, onAwaiting: () => void): LiveTurn | null {
  if (evt.type === 'subtasks') {
    return { ...prev, subtasks: evt.items || [] };
  }
  if (evt.type === 'subtask_update' && prev.subtasks) {
    return { ...prev, subtasks: withSubtaskStatus(prev.subtasks, evt.slug, evt.status) };
  }
  if (evt.type === 'tool') {
    return reduceToolEvent(prev, evt);
  }
  if (evt.type === 'message') {
    return reduceMessageEvent(prev, evt, onAwaiting);
  }
  const stepped = appendStepFor(prev, evt);
  return stepped ?? prev;
}
