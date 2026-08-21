import { createContext } from 'react';
import { CapturedRule, GateFlags } from '../api';

// ── types ──────────────────────────────────────────────────────────────────────

export type ChangeFile = { path: string; status: string; additions: number; deletions: number; diff: string };

export type AgentStep =
  | { kind: 'thought'; text: string; role?: string }
  | { kind: 'tool'; name: string; args: object; result: object; role?: string; pending?: boolean; call_id?: number }
  | { kind: 'error'; text: string; role?: string }
  | { kind: 'changes'; files: ChangeFile[]; summary: { files: number; additions: number; deletions: number } };

// `slug` and `status` are the only fields every producer sets. The label is
// `goal` for most of them and `title` for the simple-mode split-asks path, so
// both are optional and callers must fall back (see Chat.SubtaskList).
export type SubtaskItem = { slug: string; status: string; goal?: string; title?: string };

// A captured Rule / Memory / Feedback (deterministic capture pass). Rendered
// as an inline pill with change-scope / undo affordances.
export type CapturedItem = {
  id: string;
  category: string;
  scope: string;
  text: string;
  repo?: string | null;
  // Set when the captured rule LOOKS like a gate-disable request — the pill then
  // OFFERS an explicit, scoped opt-in. A gate is NEVER disabled by capture.
  gate_intent?: 'commit' | 'delete';
};

// Shared, server-truth state for captured-rule pills so undo/rescope SURVIVE a
// reload (the persisted pill hydrates from this rather than its stale step).
export type RuleState = {
  byId: Record<string, CapturedRule>;   // current persisted truth
  loaded: boolean;                       // has the index been fetched at least once
  sessionId: number | null;
  flags: GateFlags | null;               // active gate-disable flags
  refresh: () => void;
};
export const RuleStateCtx = createContext<RuleState | null>(null);

// A "live" turn: the in-progress assistant turn while streaming.
export type LiveTurn = {
  role: 'assistant';
  text: string;
  steps: AgentStep[];
  streaming: boolean;
  elapsedSec?: number;
  awaiting?: boolean;   // agent asked a question — waiting for your reply
  subtasks?: SubtaskItem[];   // Planner decomposition (team mode)
  captured?: CapturedItem[];  // Rule/Memory/Feedback captured this turn
  usage?: { pct: number; chars: number; budget: number; tokens?: number; windowTokens?: number;
            // Requests actually sent to the LLM: this turn, this chat, and the
            // machine-wide rate over the last minute.
            llmTurn?: number; llmSession?: number; llmPerMin?: number;
            // …and how many of them came back with nothing. A SUBSET of
            // llmTurn / llmPerMin, not a separate count: the requests were
            // still sent.
            llmTurnFailed?: number; llmFailedPerMin?: number };
};

export type ChatMode = 'simple' | 'plan' | 'team';

// A "builder" runs a focused single-agent interview that ends by calling a
// finalize tool. It's selected per-session and sent on EVERY message of that
// conversation (the backend reads it per-message). Launched from other views via
// a `?builder=<kind>` query param on /chat.
export type BuilderKind = 'job' | 'skill' | 'workflow' | 'rule';

// A pending human-approval gate (#1): the run is blocked until the user
// Approves/Rejects this action.
export type PendingApproval = {
  id: number;          // seq echoed back to the server
  sessionId: number;   // the session that produced it — guards wrong-session resolve
  name: string;
  args: object;
  reason?: string;
  preview?: string;
};
