import { createContext } from 'react';
import { CapturedRule, GateFlags } from '../api';
import type { AgentStep, ChangeFile, SubtaskItem, CapturedItem, LiveTurn } from './Chat.model';

export type { AgentStep, ChangeFile, SubtaskItem, CapturedItem, LiveTurn };

// ── types ──────────────────────────────────────────────────────────────────────

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
