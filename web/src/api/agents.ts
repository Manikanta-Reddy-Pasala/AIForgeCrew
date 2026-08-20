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
