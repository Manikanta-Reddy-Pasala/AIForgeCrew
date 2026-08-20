import { AgentStep, BuilderKind } from './Chat.types';

// Header overflow-menu item styles.
export const menuBtn: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left',
  background: 'none', border: 'none', padding: '7px 10px', borderRadius: 6,
  fontSize: 13, color: 'var(--fg-1)', cursor: 'pointer',
};
export const menuItem: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px',
  fontSize: 13, color: 'var(--fg-1)', cursor: 'pointer',
};

// ── Elapsed time formatter ────────────────────────────────────────────────────

export function fmtElapsed(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

export const LS_SESSION_KEY = 'aiforge.chat.activeSessionId';
export const LS_MODEL_KEY = 'aiforge.chat.model';
export const LS_MODE_KEY = 'aiforge.chat.flowmode';

// ── Builder flows ─────────────────────────────────────────────────────────────
// A "builder" runs a focused single-agent interview that ends by calling a
// finalize tool. It's selected per-session and sent on EVERY message of that
// conversation (the backend reads it per-message). Launched from other views via
// a `?builder=<kind>` query param on /chat.
export const BUILDER_KINDS: BuilderKind[] = ['job', 'skill', 'workflow', 'rule'];
export const BUILDER_LABELS: Record<BuilderKind, string> = {
  job: 'Job builder',
  skill: 'Skill builder',
  workflow: 'Workflow builder',
  rule: 'Rule builder',
};
export const BUILDER_HINTS: Record<BuilderKind, string> = {
  job: 'Interviewing you to build & schedule a recurring job',
  skill: 'Interviewing you to capture a reusable SKILL.md',
  workflow: 'Interviewing you to capture a WORKFLOW.md',
  rule: 'Interviewing you to capture a standing rule',
};
// Persist the per-session builder so it survives reload / session switch.
export const LS_BUILDER_KEY = 'aiforge.chat.builderBySession';

// ── relative time helper ──────────────────────────────────────────────────────

export function relTime(isoStr: string): string {
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

// Absolute date + time for the sidebar meta — "Jul 4, 3:14 PM" (today drops the
// date to just the time). Shown alongside the relative label.
export function dateTimeLabel(isoStr: string): string {
  const d = new Date(isoStr);
  if (isNaN(d.getTime())) return '';
  const time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (sameDay) return time;
  const date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  return `${date}, ${time}`;
}

// ── Convert a persisted ChatMsg step (from server) to AgentStep ───────────────

export function toAgentStep(raw: any): AgentStep | null {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.type === 'thought' || raw.kind === 'thought') {
    return { kind: 'thought', text: raw.text || '', role: raw.role };
  }
  if (raw.type === 'tool' || raw.kind === 'tool') {
    return { kind: 'tool', name: raw.name || '', args: raw.args || {}, result: raw.result || {}, role: raw.role };
  }
  if (raw.type === 'error' || raw.kind === 'error') {
    return { kind: 'error', text: raw.text || '' };
  }
  if (raw.type === 'changes' || raw.kind === 'changes') {
    const files = raw.files || [];
    return { kind: 'changes', files, summary: raw.summary || { files: files.length, additions: 0, deletions: 0 } };
  }
  return null;
}

// ── awaiting-reply detection (FE1) ────────────────────────────────────────────
// The agent can end a turn "awaiting" the user's reply. On the live turn that
// flag lives on liveTurn.awaiting; once persisted it must be recovered from the
// stored ChatMsg (top-level flag OR a message/awaiting step) so the affordance
// survives loadSession.
export function msgAwaiting(msg: any): boolean {
  if (!msg || typeof msg !== 'object') return false;
  if (msg.awaiting_input === true || msg.awaiting === true) return true;
  const steps: any[] = Array.isArray(msg.steps) ? msg.steps : [];
  return steps.some(s => s && typeof s === 'object' &&
    (s.awaiting_input === true || s.type === 'awaiting' ||
     ((s.type === 'message' || s.kind === 'message') && s.awaiting_input === true)));
}

// ── durable plan dismissal (FE3) ──────────────────────────────────────────────
// loadSession rehydrates the "Approve & Execute" pill from the last assistant
// message's plan_ready step on every load. Remember the message ids the user
// dismissed (per session) so the pill doesn't resurrect on reload / switch / Stop.
export const LS_DISMISSED_PLAN_PREFIX = 'aiforge.chat.dismissedPlan.';
export function getDismissedPlans(sessionId: number): Set<number> {
  try {
    const raw = localStorage.getItem(LS_DISMISSED_PLAN_PREFIX + sessionId);
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr : []);
  } catch { return new Set(); }
}
export function addDismissedPlan(sessionId: number, msgId: number): void {
  try {
    const s = getDismissedPlans(sessionId);
    s.add(msgId);
    localStorage.setItem(LS_DISMISSED_PLAN_PREFIX + sessionId, JSON.stringify([...s]));
  } catch { /* ignore */ }
}

// ── gate-intent / flag labels (CapturedPill + AutoApprovalsPanel) ─────────────
export const GATE_INTENT_FLAG: Record<string, string> = {
  commit: 'commit_auto_approve',
  delete: 'allow_delete',
};
export const GATE_INTENT_LABEL: Record<string, string> = {
  commit: 'Also stop asking before commits?',
  delete: 'Also stop asking before deletes?',
};
export const FLAG_LABEL: Record<string, string> = {
  commit_auto_approve: 'commits auto-approved',
  allow_delete: 'deletes auto-approved',
};

/** Did this assistant turn END on a stop rather than an answer?
 *
 *  Reads the STRUCTURAL marker the server stamps on a stopped turn (same shape
 *  as the `awaiting` marker above), because prose is wrong in both directions:
 *  "stopped by user" is literally what run_command returns when cancelled, so
 *  an agent quoting its own tool output looked like a stop — while a real Stop
 *  press leaves no banner at all (the loop emits an error step, and only
 *  `final_text` becomes the message content), so the case the button exists
 *  for was the one it missed. The "(stopped" prefix stays as a fallback for
 *  turns persisted before the marker existed.
 */
export function isStoppedTurn(msg?: any): boolean {
  if (!msg) return false;
  const m = typeof msg === 'string' ? { content: msg } : msg;
  const steps: any[] = Array.isArray(m.steps) ? m.steps : [];
  if (steps.some(s => s && typeof s === 'object' &&
                 (s.type === 'stopped' || s.stopped === true))) return true;
  return String(m.content ?? m.text ?? '').trim().startsWith('(stopped');
}
