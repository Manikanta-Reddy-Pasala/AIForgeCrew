// Minimal fetch wrapper against the FastAPI backend.
const BASE = '/api';

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, init);
  if (!r.ok) {
    // Read the body ONCE as text, then try to parse JSON — reading .json()
    // first and .text() in the catch double-reads the stream ("body stream
    // already read") and discards a non-JSON (proxy HTML) error body.
    let detail = '';
    try {
      const raw = await r.text();
      try { const b = JSON.parse(raw); detail = b?.detail || b?.error || raw; }
      catch { detail = raw; }
    } catch { /* ignore */ }
    const suffix = detail ? ` — ${detail}` : '';
    throw new Error(`${r.status} ${r.statusText}${suffix}`);
  }
  return r.json();
}

export const api = {
  health:   () => j<any>('/health'),
  agents:   () => j<any[]>('/agents'),
  // ── Model registry (simplified Settings) ────────────────────────
  models: () => j<{ models: RegistryModel[] }>('/agents/models'),
  addModel: (body: ModelInput) =>
    j<RegistryModel>('/agents/models', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateModel: (id: string, body: ModelInput) =>
    j<RegistryModel>(`/agents/models/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteModel: (id: string) =>
    fetch(`${BASE}/agents/models/${id}`, { method: 'DELETE' }).then(r => {
      if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
    }),
  applyModel: (id: string, roles: string[]) =>
    j<{ applied: string[]; errors: Record<string, string> }>(`/agents/models/${id}/apply`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ roles }),
    }),
  syncModels: () => j<{ added: string[]; count: number }>('/agents/models/sync', { method: 'POST' }),

  llmSettings: () =>
    j<LlmSettings>('/runtime/llm-settings'),
  setLlmSettings: (vals: Partial<LlmSettings>) =>
    j<LlmSettings>('/runtime/llm-settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(vals),
    }),
  tickets:  (qs: Record<string, string> = {}) =>
    j<any[]>(`/tickets?${new URLSearchParams(qs).toString()}`),
  ticket:   (id: string) => j<any>(`/tickets/${id}`),
  create:   (body: any) => j<any>('/tickets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  patch:    (id: string, body: any) => j<any>(`/tickets/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  delete:   (id: string) => j<any>(`/tickets/${id}`, {
    method: 'DELETE',
  }),
  resetTickets: () => j<{ ok: boolean; deleted: number }>('/tickets/reset', { method: 'POST' }),
  resetChats:   () => j<{ ok: boolean; deleted: number }>('/chat/sessions/reset', { method: 'POST' }),
  libraryList:   (kind: string) => j<any[]>(`/library/${kind}`),
  libraryCreate: (kind: string, body: any) => j<any>(`/library/${kind}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }),
  libraryGenerate: (kind: string, prompt: string) => j<{ ok: boolean; draft: string }>(`/library/${kind}/generate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt }),
  }),
  comment:  (id: string, body: string) => j<any>(`/tickets/${id}/comments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body, author: 'human' }),
  }),
  runParallel: (id: string) =>
    j<{ started: boolean; subtasks: number }>(`/tickets/${id}/run-parallel`, { method: 'POST' }),
  memoryStats:  () => j<any>('/memory/stats'),
  memorySearch: (q: string, role = 'planner', topK = 12) =>
    j<any[]>(`/memory/search?q=${encodeURIComponent(q)}&role=${role}&top_k=${topK}`),
  // Markdown-file memory (human-readable notes on disk + searchable)
  memoryFiles: () => j<any[]>('/memory/files'),
  memoryFileGet: (name: string) => j<any>(`/memory/files/${encodeURIComponent(name)}`),
  memoryFileCreate: (body: { title: string; text: string; kind?: string; tags?: string[] }) =>
    j<any>('/memory/files', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  memoryFilesIngest: () => j<any>('/memory/files/ingest', { method: 'POST' }),
  memoryFilesCompact: (opts?: { group_by?: string; dry_run?: boolean; summarize?: boolean }) => {
    const p = new URLSearchParams();
    if (opts?.group_by) p.set('group_by', opts.group_by);
    if (opts?.dry_run) p.set('dry_run', 'true');
    if (opts?.summarize === false) p.set('summarize', 'false');
    return j<{ ok: boolean; dry_run: boolean; group_by: string;
      groups: Record<string, number>; files_in: number; files_out: number;
      compacted?: string[]; summarized?: string[]; archive?: string; note?: string }>(
      `/memory/files/compact?${p.toString()}`, { method: 'POST' });
  },
  memoryFileDelete: (name: string) =>
    j<any>(`/memory/files/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  memorySources: () => j<MemorySource[]>('/memory/sources'),
  memorySourceCreate: (body: { kind: string; location: string; name?: string }) =>
    j<MemorySource>('/memory/sources', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  memorySourceDelete: (id: number) =>
    fetch(`${BASE}/memory/sources/${id}`, { method: 'DELETE' }).then(r => {
      if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
    }),
  memorySourceIndex: (id: number) =>
    j<MemorySource>(`/memory/sources/${id}/index`, { method: 'POST' }),
  memorySourceUpload: async (file: File, name?: string): Promise<MemorySource> => {
    const fd = new FormData();
    fd.append('file', file);
    if (name) fd.append('name', name);
    const r = await fetch(`${BASE}/memory/sources/upload`, { method: 'POST', body: fd });
    if (!r.ok) {
      let detail = '';
      try { const b = await r.json(); detail = b?.detail || b?.error || ''; } catch { /* ignore */ }
      throw new Error(`${r.status} ${r.statusText}${detail ? ` — ${detail}` : ''}`);
    }
    return r.json();
  },
  // Memory admin — per-datasource overview + DESTRUCTIVE clear (confirm-guarded).
  memoryOverview: () => j<MemoryOverview>('/memory/overview'),
  memoryClearStore: (store: string) =>
    j<any>(`/memory/clear/${encodeURIComponent(store)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    }),
  memoryClearAll: () =>
    j<any>('/memory/clear-all', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    }),
  mcpTool:  (tool: string, args: Record<string, any> = {}) =>
    j<any>('/mcp/tool', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool, args }),
    }),
  chatRetain: (p: {
    query: string; answer: string; worked: boolean;
    topic?: string; hit_refs?: string[];
  }) => j<any>('/chat/retain', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(p),
  }),
  chatAsk: (query: string, topK = 12) => j<any>('/chat/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, top_k: topK, role: 'planner' }),
  }),
  agentConfig:    () => j<any>('/config/agents'),
  setAgentConfig: (role: string, provider: string, model: string) =>
    j<any>(`/config/agents/${role}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, model }),
    }),

  // ── v2 archetype config (used by the Settings page) ─────────────
  // GET  → { [role]: AgentRoleConfig }   (5 v5 archetypes)
  // GET  → ProviderCatalog[]             (3 providers + their models)
  // PUT  → AgentRoleConfig (echo)        (404 unknown role / 400 bad input)
  agentsV2Config:    () => j<Record<AgentRole, AgentRoleConfig>>(
    '/agents/v2/config',
  ),
  agentsV2Providers: () => j<ProviderCatalog[]>('/agents/v2/providers'),
  setAgentV2Config:  (role: AgentRole, body: AgentRoleConfigInput) =>
    j<AgentRoleConfig & { role: AgentRole }>(`/agents/v2/${role}/config`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // Profile presets — bulk-assign one (provider, model) to all 9 archetypes.
  agentsV2Profiles: () => j<{
    profiles: Array<{ name: string; provider: string; model: string }>;
  }>('/agents/v2/profiles'),
  applyAgentV2Profile: (name: string) =>
    j<{ profile: string; roles: Record<AgentRole, AgentRoleConfig> }>(
      `/agents/v2/profile/${name}`,
      { method: 'PUT' },
    ),
  resetAgentsV2: (keepDefault = false) =>
    j<{ ok: boolean; removed?: boolean | string; path?: string; note?: string }>(
      `/agents/v2/reset${keepDefault ? '?keep_default=true' : ''}`,
      { method: 'POST' },
    ),

  getForceFullPipeline: () =>
    j<{ enabled: boolean }>('/runtime/force_full_pipeline'),
  setForceFullPipeline: (enabled: boolean) =>
    j<{ enabled: boolean; persisted: boolean }>('/runtime/force_full_pipeline', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }),

  // Test an OpenAI-compatible endpoint reachability. `insecure_tls` skips
  // TLS verification for this probe (self-signed / internal HTTPS box).
  // `role` lets the server fill a blank base_url/api_key from that role's
  // saved config — so Test works after Save without re-typing the token.
  providersTest: (base_url: string, api_key?: string, insecure_tls?: boolean,
                  role?: string) =>
    j<{ ok: boolean; models?: string[]; error?: string }>('/providers/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_url, api_key, insecure_tls: !!insecure_tls, role }),
    }),

  // Workflow registry + route detection
  workflows: () => j<WorkflowSpec[]>('/workflows'),
  workflowPreview: (body: string, opts: {
    title?: string;
    attachments?: string[];
    intent?: any;
  } = {}) => j<RoutePreview>('/workflows/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      body,
      title: opts.title || '',
      attachments: opts.attachments || [],
      intent: opts.intent || null,
    }),
  }),
  setRoute: (id: string, route: 'code' | 'workflow',
             workflowId?: string) =>
    j<any>(`/tickets/${id}/route`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        route,
        route_workflow: workflowId || null,
        route_source: 'manual',
        route_confidence: 1.0,
      }),
    }),

  // ── Scheduled jobs (NL → cron → recurring tickets) ───────────────
  previewJob: (instructions: string) =>
    j<JobPreview>('/jobs/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instructions }),
    }),
  createJob: (draft: JobDraft) =>
    j<Job>('/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(draft),
    }),
  listJobs: () => j<Job[]>('/jobs'),
  patchJob: (id: number, patch: Partial<JobDraft & { enabled: boolean }>) =>
    j<Job>(`/jobs/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  deleteJob: (id: number) =>
    j<{ ok: boolean }>(`/jobs/${id}`, { method: 'DELETE' }),
  runJobNow: (id: number) =>
    j<{ ok: boolean; job: Job }>(`/jobs/${id}/run-now`, { method: 'POST' }),
};

// ── memory source types ───────────────────────────────────────────

export interface MemorySource {
  id: number;
  kind: string;
  name: string;
  location: string;
  status: string;
  units: number;
  error: string | null;
  last_indexed: string | null;
  created_at: string;
}

// ── memory overview (admin) types ─────────────────────────────────

export interface MemoryStoreSection {
  available?: boolean;
  reason?: string;
  // graph stores
  labels?: Record<string, number>;
  total?: number;
  relationships?: Record<string, number>;
  count?: number;
  // sqlite
  by_kind?: Record<string, number>;
  path?: string;
  // md
  bytes?: number;
  dir?: string;
  // chat
  sessions?: number;
  messages?: number;
  // sources
  by_status?: Record<string, number>;
  items?: MemorySource[];
}

export interface MemoryOverview {
  backend: string;
  stores: Record<string, MemoryStoreSection>;
}

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
}

export interface RegistryModel {
  id: string;
  label: string;
  model: string;
  base_url: string;
  insecure_tls: boolean;
  vision: 'auto' | 'yes' | 'no';
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
  context_window?: number;
}

export interface AgentRoleConfigInput {
  provider: ProviderId;
  model: string;
  base_url?: string | null;
  api_key?: string | null;
  insecure_tls?: boolean;
}

// ── scheduled job types ───────────────────────────────────────────

export interface JobDraft {
  name: string;
  cron: string;
  ticket_title: string;
  ticket_body: string;
  project: string | null;
}

export interface JobPreview {
  ok: boolean;
  error?: string;
  draft?: JobDraft;
  human_schedule?: string;
  next_runs?: string[];
}

export interface Job extends JobDraft {
  id: number;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string;
  last_error: string | null;
  created_at: string;
  human_schedule: string;
}

// ── workflow types ────────────────────────────────────────────────

export interface WorkflowSpec {
  id: string;
  label: string;
  description: string;
  triggers: Record<string, any>;
  required_attachments: string[];
  optional_inputs: string[];
  tags: string[];
}

export interface RouteCandidate {
  workflow_id: string;
  label: string;
  score: number;
  threshold: number;
  above_threshold: boolean;
  reasons: string[];
}

export interface RouteChosen {
  kind: 'code' | 'workflow';
  workflow_id: string | null;
  confidence: number;
  source: 'auto' | 'manual';
  rationale: string;
}

export interface RoutePreview {
  chosen: RouteChosen;
  candidates: RouteCandidate[];
}

export function logStreamURL(role: string): string {
  return `${BASE}/logs/${role}/stream`;
}

export function chatAgentURL(): string {
  return `${BASE}/chat/agent`;
}

// ── Chat session types ────────────────────────────────────────────

export interface ChatSession {
  id: number;
  title: string;
  cwd: string | null;
  role?: string | null;
  created_at: string;
  updated_at: string;
  message_count?: number;
}

export interface ChatModelEntry {
  id: string;
  label: string;
  active: boolean;
}

export interface ChatModelsResponse {
  provider: string;
  current: string | null;
  current_active: boolean;
  models: ChatModelEntry[];
}

export interface ChatMsg {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  steps: any[];
  created_at: string;
}

// ── Chat session API methods ──────────────────────────────────────

export const chatApi = {
  sessions: () => j<ChatSession[]>('/chat/sessions'),

  // ── Model registry — Settings page calls these via chatApi (the methods
  // live on `api`; delegate so `chatApi.addModel`/`syncModels`/etc. resolve
  // instead of throwing "$.addModel is not a function").
  models: api.models,
  addModel: api.addModel,
  updateModel: api.updateModel,
  deleteModel: api.deleteModel,
  applyModel: api.applyModel,
  syncModels: api.syncModels,
  providersTest: api.providersTest,

  chatModels: () => j<ChatModelsResponse>('/chat/models'),

  setChatModel: (model: string, provider?: string) =>
    j<{ provider: string; model: string; active: boolean }>('/chat/model', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, ...(provider ? { provider } : {}) }),
    }),

  // (Re)load a model on the LM Studio host at a chosen context window.
  reloadModel: (model: string, context_length: number, ttl?: number) =>
    j<{ ok: boolean; model: string; context_length: number; parallel: number; ttl: number }>(
      '/chat/model/reload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, context_length, ...(ttl ? { ttl } : {}) }),
    }),

  orchestratorModel: () =>
    j<{ provider: string; model: string; roles: string[]; models: { id: string; label: string }[] }>('/chat/orchestrator-model'),
  setOrchestratorModel: (model: string, provider?: string) =>
    j<{ ok: boolean; model: string; roles: string[] }>('/chat/orchestrator-model', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, ...(provider ? { provider } : {}) }),
    }),

  sessionCreate: (body?: { title?: string; cwd?: string }) =>
    j<ChatSession>('/chat/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }),

  sessionGet: (id: number) =>
    j<{ session: ChatSession; messages: ChatMsg[] }>(`/chat/sessions/${id}`),

  sessionRename: (id: number, title: string) =>
    j<ChatSession>(`/chat/sessions/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    }),

  sessionDelete: (id: number) =>
    fetch(`${BASE}/chat/sessions/${id}`, { method: 'DELETE' }).then(r => {
      if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
    }),

  // Resolve a pending approval gate (#1) — Approve/Reject a risky action.
  approve: (id: number, decision: 'approve' | 'reject', seq?: number) =>
    j<{ resolved: boolean; decision: string }>(`/chat/sessions/${id}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, ...(seq != null ? { id: seq } : {}) }),
    }),

  // Workspace checkpoints (#3).
  checkpoints: (id: number) =>
    j<{ checkpoints: Array<{ sha: string; label: string; when: string }> }>(
      `/chat/sessions/${id}/checkpoints`),

  checkpointRestore: (id: number, sha: string,
                      opts?: { paths?: string[]; delete_orphans?: boolean }) =>
    j<{ ok: boolean; restored?: string; left_in_place?: string[];
        deleted?: string[]; error?: string }>(
      `/chat/sessions/${id}/checkpoints/restore`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sha, ...(opts || {}) }),
      }),
};

export type ConfluenceCfg = {
  base_url: string; user: string; insecure_tls: boolean;
  has_token: boolean; env_managed: boolean;
};

export type JiraCfg = ConfluenceCfg;

export type GitlabCfg = {
  base_url: string; project: string; oauth: boolean; insecure_tls: boolean;
  has_token: boolean; env_managed: boolean;
};

export type McpCatalogEntry = {
  id: string; name: string; description: string; transport: string;
  url: string; homepage?: string; needs_api_key?: boolean; category?: string;
  installable: boolean;
};
export type McpServer = {
  id: string; name: string; url: string; transport: string; enabled: boolean;
  catalog_id: string; description: string; api_key_set: boolean;
};

export const mcpApi = {
  catalog: () => j<{ catalog: McpCatalogEntry[] }>('/mcp/catalog'),
  servers: () => j<{ servers: McpServer[] }>('/mcp/servers'),
  install: (body: { catalog_id: string; url?: string; name?: string; api_key?: string }) =>
    j<McpServer>('/mcp/servers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  update: (id: string, body: Partial<{ name: string; url: string; description: string; enabled: boolean; api_key: string }>) =>
    j<McpServer>(`/mcp/servers/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  remove: (id: string) =>
    fetch(`${BASE}/mcp/servers/${id}`, { method: 'DELETE' }).then(r => {
      if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
    }),
  test: (id: string) =>
    j<{ ok: boolean; endpoint?: string; tools?: { name: string; description: string }[]; error?: string }>(
      `/mcp/servers/${id}/test`, { method: 'POST' }),
};

export const integrationsApi = {
  getConfluence: () => j<ConfluenceCfg>('/integrations/confluence'),
  setConfluence: (cfg: { base_url?: string; token?: string; user?: string; insecure_tls?: boolean }) =>
    j<ConfluenceCfg>('/integrations/confluence', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    }),
  testConfluence: () =>
    j<{ ok: boolean; base_url?: string; auth?: string; error?: string; detail?: string; hint?: string; denied_reason?: string }>(
      '/integrations/confluence/test', { method: 'POST' }),

  getJira: () => j<JiraCfg>('/integrations/jira'),
  setJira: (cfg: { base_url?: string; token?: string; user?: string; insecure_tls?: boolean }) =>
    j<JiraCfg>('/integrations/jira', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    }),
  testJira: () =>
    j<{ ok: boolean; base_url?: string; auth?: string; user?: string; error?: string; detail?: string; hint?: string; denied_reason?: string }>(
      '/integrations/jira/test', { method: 'POST' }),

  getGitlab: () => j<GitlabCfg>('/integrations/gitlab'),
  setGitlab: (cfg: { base_url?: string; token?: string; project?: string; oauth?: boolean; insecure_tls?: boolean }) =>
    j<GitlabCfg>('/integrations/gitlab', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    }),
  testGitlab: () =>
    j<{ ok: boolean; base_url?: string; auth?: string; user?: string; error?: string; detail?: string; hint?: string }>(
      '/integrations/gitlab/test', { method: 'POST' }),
};

export function chatSessionMessageURL(id: number): string {
  return `${BASE}/chat/sessions/${id}/message`;
}

// Re-attach to an in-flight run after navigating back to the Chat view. The
// SSE stream replays the run's buffered events then tails live ones. The first
// event is {type:'attached', running} so the client knows if anything is live.
export function chatSessionAttachURL(id: number): string {
  return `${BASE}/chat/sessions/${id}/attach`;
}

// ── Chat image attachments ────────────────────────────────────────────────
export interface ChatMedia {
  id: number; session_id: number; filename: string; path: string;
  mime: string; description: string; created_at: string; auto_described?: boolean;
}
export function chatMediaUpload(sessionId: number, file: File): Promise<ChatMedia> {
  const fd = new FormData();
  fd.append('file', file);
  return fetch(`${BASE}/chat/sessions/${sessionId}/media`, { method: 'POST', body: fd })
    .then(async r => { if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `upload failed (${r.status})`); return r.json(); });
}
export function chatMediaList(sessionId: number): Promise<{ media: ChatMedia[]; vision: boolean }> {
  return j(`/chat/sessions/${sessionId}/media`);
}
export function chatMediaDescribe(mediaId: number, description: string): Promise<ChatMedia> {
  return j(`/chat/media/${mediaId}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ description }),
  });
}
export function chatMediaDelete(mediaId: number): Promise<void> {
  return fetch(`${BASE}/chat/media/${mediaId}`, { method: 'DELETE' }).then(r => {
    if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
  });
}
export function chatMediaRawURL(mediaId: number): string {
  return `${BASE}/chat/media/${mediaId}/raw`;
}

// Stop the in-flight run for a session (halts agents + kills subprocesses).
export function chatSessionStop(id: number): Promise<{ stopped: boolean }> {
  return fetch(`${BASE}/chat/sessions/${id}/stop`, { method: 'POST' })
    .then(r => r.ok ? r.json() : { stopped: false })
    .catch(() => ({ stopped: false }));
}

// Force-reset ALL chat runs (kill all) — recovers from a wedged run that left a
// session stuck busy or a new chat waiting on the team run lock.
export function chatKillAll(): Promise<{ killed: number[]; count: number; team_lock_released: boolean }> {
  return fetch(`${BASE}/chat/kill-all`, { method: 'POST' })
    .then(r => r.ok ? r.json() : { killed: [], count: 0, team_lock_released: false })
    .catch(() => ({ killed: [], count: 0, team_lock_released: false }));
}

// Steer the IN-FLIGHT run without stopping it (Gap A — mid-run steering).
// The message is queued and folded into the agent's context at its next step.
export function chatSessionSteer(id: number, content: string): Promise<{ queued: boolean; unsupported?: boolean; reason?: string }> {
  return fetch(`${BASE}/chat/sessions/${id}/steer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
    .then(r => r.ok ? r.json() : { queued: false })
    .catch(() => ({ queued: false }));
}

// ── Rule / Memory / Feedback capture transparency ──────────────────────────
export type AppliedFlag = {
  name: string;
  scope: string;
  repo?: string | null;
  session_id?: string | null;
};

export type CapturedRule = {
  id: string;
  category: string;
  scope: string;
  canonical?: string;
  text?: string;
  repo?: string | null;
  applied_flags?: AppliedFlag[];
};

export function rules(params?: { repo?: string; session_id?: number }):
  Promise<{ items: CapturedRule[]; by_scope: Record<string, CapturedRule[]> }> {
  const qs = new URLSearchParams();
  if (params?.repo) qs.set('repo', params.repo);
  if (params?.session_id != null) qs.set('session_id', String(params.session_id));
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  return j(`/rules${suffix}`);
}

export function setRuleScope(id: string, scope: string): Promise<any> {
  return j(`/rules/${id}/scope`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scope }),
  });
}

export function deleteRule(id: string): Promise<{ ok: boolean }> {
  return fetch(`${BASE}/rules/${id}`, { method: 'DELETE' })
    .then(r => r.ok ? r.json() : { ok: false })
    .catch(() => ({ ok: false }));
}

// ── Explicit gate-disable flags (auto-approvals) ───────────────────────────
// A gate is only ever disabled by an explicit user action through these — never
// by the classifier. The capture pill merely OFFERS the opt-in.
export type GateFlags = {
  by_scope: {
    global: Record<string, boolean>;
    repo: Record<string, Record<string, boolean>>;
    session: Record<string, Record<string, boolean>>;
  };
};

export function ruleFlags(): Promise<GateFlags> {
  return j('/rules/flags');
}

export function setGateFlag(
  name: string, scope: string,
  opts?: { repo?: string; session_id?: number; rule_id?: string; allow_global?: boolean },
): Promise<{ ok: boolean; applied: boolean; reason?: string }> {
  return j('/rules/flags', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, scope, ...(opts || {}) }),
  });
}

export function clearGateFlag(
  name: string, scope: string, opts?: { repo?: string; session_id?: number },
): Promise<{ ok: boolean }> {
  const qs = new URLSearchParams({ scope });
  if (opts?.repo) qs.set('repo', opts.repo);
  if (opts?.session_id != null) qs.set('session_id', String(opts.session_id));
  return fetch(`${BASE}/rules/flags/${encodeURIComponent(name)}?${qs.toString()}`,
    { method: 'DELETE' })
    .then(r => r.ok ? r.json() : { ok: false })
    .catch(() => ({ ok: false }));
}

export function chatSessionTicket(
  id: number,
  content: string,
  project?: string,
): Promise<{ ticket: string; ticket_id: number; project: string | null; trace_url: string }> {
  return j(`/chat/sessions/${id}/ticket`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, ...(project ? { project } : {}) }),
  });
}

export function traceStreamURL(identifier: string): string {
  // DB-sourced ticket-event stream (works across api/runner containers,
  // surfaces clarification + status); not the log-tail trace.
  return `${BASE}/tickets/${identifier}/events/stream`;
}

/** Poll ticket status — returns the raw ticket object including metadata. */
export function ticketStatus(identifier: string): Promise<{ ticket: { status: string; metadata?: Record<string, any> } }> {
  return j(`/tickets/${identifier}`);
}

/** Submit a clarification answer. Returns the re-queued ticket plus a trace_url for resuming. */
export function ticketAnswer(
  identifier: string,
  content: string,
): Promise<{ ticket: string; status: string; trace_url: string }> {
  return j(`/tickets/${identifier}/answer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}
