/** Plain-words facts about a turn, read from what the server already sent and
 *  saved with it: why it stopped, what it cost, which agents worked, how long a
 *  step took. Pure functions, so the chat can show them after a reload too. */

type Raw = Record<string, any>;

const REASONS: Record<string, string> = {
  llm_unavailable: 'the model could not be reached',
  llm_request_fails: 'the model kept failing',
  approval_timeout: 'an approval waited too long',
  deadline: 'the turn hit its time limit',
  paused: 'it was paused',
  no_implementation: 'no change was made',
  pipeline_error: 'the team pipeline failed',
  server_restart: 'the server restarted during the run',
  context_thrash: 'the context kept filling up faster than the work progressed',
};

/** Why a stopped turn stopped, in words, or '' when there is nothing to add.
 *  Only a specific reason is told: the save's own marker ("cancelled",
 *  "guard") cannot tell a Stop press from a stop banner, and the answer
 *  already says "(stopped …" — repeating it in other words would mislead. */
export function stopReason(steps: Raw[] | undefined | null): string {
  const pick = (steps ?? []).find(s => s && typeof s === 'object' && s.type === 'stopped'
    && s.reason && s.reason !== 'guard' && s.reason !== 'cancelled');
  if (!pick) return '';
  const why = REASONS[String(pick.reason)] ?? String(pick.reason).replace(/_/g, ' ');
  const err = typeof pick.error === 'string' && pick.error.trim()
    ? ` — ${pick.error.trim().replace(/\s+/g, ' ').slice(0, 160)}` : '';
  return `Stopped: ${why}${err}`;
}

/** The turn's request count and tokens, from the "⚡ N LLM requests …" line the
 *  server saves with it. '' when there is none. */
export function usageLine(steps: Raw[] | undefined | null): string {
  for (let i = (steps ?? []).length - 1; i >= 0; i--) {
    const s = (steps as Raw[])[i];
    const t = s && typeof s.text === 'string' ? s.text : '';
    if (/^\s*⚡\s*\d+ LLM requests?/.test(t)) {
      return t.trim().replace(/\s+for this message/, '').replace(/\s+/g, ' ');
    }
  }
  return '';
}

/** The team pipeline's agents. A subtask's slug or a reply label is a role
 *  too, but not a stage. */
const AGENTS = new Set(['triage', 'enhancer', 'researcher', 'architect', 'planner', 'verifier',
  'doer', 'refiner', 'feedback', 'reviewer', 'validator', 'learner', 'tester']);

/** The agents that worked in this turn, in the order they first appeared.
 *  Fewer than two means it was one agent: no strip to show. */
export function stages(steps: Raw[] | undefined | null): string[] {
  const seen: string[] = [];
  for (const s of steps ?? []) {
    const r = s && typeof s.role === 'string' ? s.role.trim().toLowerCase() : '';
    if (!AGENTS.has(r) || seen.includes(r)) continue;
    seen.push(r);
  }
  return seen.length >= 2 ? seen : [];
}

/** 0.4 → "0.4s", 12.3 → "12s", 75 → "1m 15s". */
export function fmtSecs(secs: number): string {
  if (!Number.isFinite(secs) || secs < 0) return '';
  if (secs < 9.95) return `${Math.round(secs * 10) / 10}s`;
  const whole = Math.round(secs);
  if (whole < 60) return `${whole}s`;
  const m = Math.floor(whole / 60);
  return `${m}m ${whole - m * 60}s`;
}

/** What a plan holds, from its own text: numbered or bulleted lines are
 *  steps, `name.ext` tokens are files. `short` is "5 steps · 3 files" ('' when
 *  the plan names neither); `files` lists the file names. */
export function planFacts(plan: string | undefined | null): { short: string; files: string[] } {
  const text = String(plan ?? '');
  // Top-level items only: a nested bullet is detail of the step above it.
  const steps = text.split('\n').filter(l => /^(?:\d+[.)]|[-*•])\s+\S/.test(l.replace(/^ {0,1}/, ''))).length;
  const files: string[] = [];
  for (const m of text.matchAll(/[\w./-]*[\w-]+\.(?:py|tsx?|jsx?|java|kt|go|rs|rb|md|json|ya?ml|toml|sql|css|html|sh)\b/g)) {
    const name = m[0].split('/').pop() ?? m[0];
    if (!m[0].includes('/') && /^[A-Z][a-z]+\.js$/.test(name)) continue;   // "Node.js" is a product
    if (!files.includes(name)) files.push(name);
  }
  const parts: string[] = [];
  if (steps) parts.push(`${steps} step${steps === 1 ? '' : 's'}`);
  if (files.length) parts.push(`${files.length} file${files.length === 1 ? '' : 's'}`);
  return { short: parts.join(' · '), files };
}
