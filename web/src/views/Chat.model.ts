// The chat's live-turn model: plain types with no imports, shared by the web
// UI and the VS Code extension (packages/aiforge_vscode), which bundles
// Chat.reduce.ts and must not pull React or the web API client in with it.

export type ChangeFile = { path: string; status: string; additions: number; deletions: number; diff: string };

export type AgentStep =
  // `streamed`: reply text the model streamed that the run then set aside
  // (a gate re-prompted it, a tool call followed) — kept, not wiped.
  | { kind: 'thought'; text: string; role?: string; streamed?: boolean }
  | { kind: 'tool'; name: string; args: object; result: object; role?: string; pending?: boolean; call_id?: number }
  | { kind: 'error'; text: string; role?: string }
  // A supplementary report (a team member's extra message) shown inline.
  // role 'reply': the answer to a message the user sent mid-run (`to`).
  | { kind: 'message'; text: string; role?: string; to?: string }
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

// A "live" turn: the in-progress assistant turn while streaming.
export type LiveTurn = {
  role: 'assistant';
  text: string;
  steps: AgentStep[];
  streaming: boolean;
  // The reply as the model writes it ('delta' events): shown until the step
  // resolves into a tool call or the final message replaces it. `draft` is the
  // tail of a tool step / reasoning being written, shown as a muted line.
  streamText?: string;
  draft?: string;
  elapsedSec?: number;
  // From the server heartbeat while the run says nothing: how long it has been
  // quiet and what it is doing. Cleared by the next real event.
  quiet?: { seconds: number; phase: string; lost?: boolean };
  // The answer to "what is the status?" typed while the run goes on: built by
  // the server from the run itself, shown at once, replaced by the next one.
  status?: { question: string; text: string };
  awaiting?: boolean;   // agent asked a question — waiting for your reply
  subtasks?: SubtaskItem[];   // Planner decomposition (team mode)
  captured?: CapturedItem[];  // Rule/Memory/Feedback captured this turn
  usage?: { pct: number; chars: number; budget: number; tokens?: number; windowTokens?: number;
    compactAtTokens?: number; compactPct?: number; windowSource?: string;
    // Team/pipeline turns: the stage whose context this reading is (doer, planner…).
    stage?: string;
            // Requests actually sent to the LLM: this turn, this chat, and the
            // machine-wide rate over the last minute.
            llmTurn?: number; llmSession?: number; llmPerMin?: number;
            // …and how many of them came back with nothing. A SUBSET of
            // llmTurn / llmPerMin, not a separate count: the requests were
            // still sent.
            llmTurnFailed?: number; llmFailedPerMin?: number;
            // Tokens the model WROTE for this message (provider-reported).
            llmTurnTokensOut?: number };
};
