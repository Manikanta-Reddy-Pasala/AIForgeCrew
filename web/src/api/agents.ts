// ── agent v2 config types ─────────────────────────────────────────

export type AgentRole =
  | 'enhancer' | 'architect' | 'planner' | 'verifier' | 'doer' | 'feedback' | 'learner';

export type ProviderId = 'openai_compatible';
export type ModelTier = 'fast' | 'balanced' | 'premium';

export interface ProviderModel {
  id: string;
  label: string;
  context: number | null;
  tier: ModelTier | null;
}

export interface ProviderCatalog {
  id: ProviderId;
  label: string;
  default_model: string;
  models: ProviderModel[];
}

export interface AgentRoleConfig {
  provider: ProviderId;
  model: string;
  base_url: string | null;
  api_key_set?: boolean;
  insecure_tls?: boolean;
}
export interface LlmSettings {
  max_output_tokens: number;
  context_window: number;
  vision_capable: number;
  cave_mode: number;
  compact_llm: number;
  ctx_no_recall: number;
  ctx_no_mentions: number;
  ctx_no_skills: number;
  ctx_no_workflows: number;
  ctx_no_repomap: number;
  ctx_no_summary: number;
  // Per-turn chat budget guards (runaway guards, not task budgets).
  chat_safety_cap: number;
  chat_turn_deadline_s: number;
  chat_cap_extensions: number;
  chat_unattended_cap: number;
  llm_max_rpm: number;
  /** seconds to wait after the PROVIDER rejects us for sending too fast and
   *  sends no Retry-After (0 = ordinary exponential backoff) */
  llm_rate_limit_backoff_s: number;
  /** the most a single rejection may cost — this caller's wait AND the
   *  process-wide hold. Retry-After is a number a remote server chose. */
  llm_rate_limit_cap_s: number;
}

// PUT body: any subset of the knobs, plus names to FORGET so they fall back to
// the env var / built-in default.
export type LlmSettingsInput = Partial<LlmSettings> & { unset?: string[] };

export interface RegistryModel {
  id: string;
  label: string;
  model: string;
  base_url: string;
  insecure_tls: boolean;
  vision: 'auto' | 'yes' | 'no';
  thinking: 'auto' | 'yes' | 'no';
  has_vision: boolean;
  has_thinking: boolean;
  context_window: number;
  api_key_set: boolean;
}
export interface ModelInput {
  label?: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  insecure_tls?: boolean;
  vision?: 'auto' | 'yes' | 'no';
  thinking?: 'auto' | 'yes' | 'no';
  context_window?: number;
}

export interface AgentRoleConfigInput {
  provider: ProviderId;
  model: string;
  base_url?: string | null;
  api_key?: string | null;
  insecure_tls?: boolean;
}

/** Machine-wide LLM request meter (GET /api/llm/usage).
 *  Rolling windows, NOT cumulative buckets: `last_60m` counts calls in the
 *  last hour, `total` counts every call since the API process started. */
export interface LlmUsage {
  total: number;
  per_minute: number;
  last_15m: number;
  last_60m: number;
  by_role: Record<string, number>;
  by_provider: Record<string, number>;
  by_model: Record<string, number>;
  /** How many of the requests in each window came back with NO answer —
   *  a subset of the counts above, never a separate population. A failed
   *  request still went out and still counted against the provider's rate
   *  limit, so `per_minute` keeps it; this says how much of that rate is a
   *  retry storm rather than work. */
  failed: number;
  failed_per_minute: number;
  failed_15m: number;
  failed_60m: number;
  /** failure label -> count over the last hour (http_500, timeout, cancelled,
   *  empty, …) */
  by_fail_reason: Record<string, number>;
  /** Tokens as the PROVIDER reported them, never estimated. `tokens_out` is
   *  what the model wrote — the number a "be brief" instruction moves and the
   *  request count cannot show. */
  tokens_in: number;
  tokens_out: number;
  tokens_out_15m: number;
  tokens_out_60m: number;
  tokens_in_60m: number;
  tokens_out_by_role: Record<string, number>;
  uptime_s: number;
  /** the 60s rate buffer overflowed WITHIN the last minute — `per_minute` is
   *  a floor. The wider windows come from minute buckets and stay exact. */
  rate_capped: boolean;
  /** operator ceiling on requests/min (0 = none) and callers waiting on it */
  limit_rpm: number;
  queued: number;
  /** requests counted against the ceiling in the last 60s (its own count, not
   *  `per_minute`: the ceiling also counts sends the meter has no token for) */
  limit_used?: number;
  /** seconds left on a hold a PROVIDER imposed by rejecting us (429/quota).
   *  Distinct from `queued`, which is our own ceiling throttling us — and it
   *  applies even at limit_rpm=0, which is why the badge needs it to explain
   *  itself. */
  held_s?: number;
  series_60m?: number[];
  /** failures per minute, same 60 slots and same indexes as `series_60m` */
  series_fail_60m?: number[];
  /** tokens WRITTEN per minute, same 60 slots and indexes as `series_60m` */
  series_token_out_60m?: number[];
}
