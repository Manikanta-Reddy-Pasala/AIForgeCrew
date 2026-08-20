import { j, apiFetch } from './core';
import type {
  RegistryModel, ModelInput, LlmSettings, LlmSettingsInput,
  AgentRole, AgentRoleConfig, AgentRoleConfigInput, ProviderCatalog, LlmUsage,
} from './agents';
import type { MemorySource, MemoryOverview } from './memory';
import type { WorkflowSpec, RoutePreview } from './workflows';
import type { JobPreview, JobDraft, Job } from './jobs';

export const api = {
  health:   () => j<any>('/health'),
  // Machine-wide LLM request meter for the toolbar badge. `series=false` skips
  // the 60-bucket sparkline when the panel is closed and nothing draws it.
  llmUsage: (series = true) =>
    j<LlmUsage>(`/llm/usage?series=${series ? 'true' : 'false'}`),
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
    apiFetch(`/agents/models/${id}`, { method: 'DELETE' }).then(r => {
      if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
    }),
  applyModel: (id: string, roles: string[]) =>
    j<{ applied: string[]; errors: Record<string, string> }>(`/agents/models/${id}/apply`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ roles }),
    }),
  syncModels: () => j<{ added: string[]; count: number }>('/agents/models/sync', { method: 'POST' }),
  // Per-chat-mode approval toggles (Chat / Plan / Pipeline). true = that mode
  // pauses for human Approve/Reject on ask-policy / review-gated tools.
  approvalSettings: () =>
    j<{ chat: boolean; plan: boolean; pipeline: boolean }>('/chat/approval-settings'),
  setApprovalMode: (mode: 'chat' | 'plan' | 'pipeline', enabled: boolean) =>
    j<{ chat: boolean; plan: boolean; pipeline: boolean }>(`/chat/approval-settings/${mode}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }),
  // Capability-based auto-assignment: thinking→reasoning model, coder→fast coder,
  // vision→vision model. Applies to every archetype internally.
  autoAssign: () =>
    j<{ assignments: Record<string, string>; applied: boolean }>('/agents/auto-assign', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }),

  llmSettings: () =>
    j<LlmSettings>('/runtime/llm-settings'),
  setLlmSettings: (vals: LlmSettingsInput) =>
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
  libraryDelete: (kind: string, name: string) =>
    j<any>(`/library/${kind}/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  libraryClear: (kind: string) => j<{ ok: boolean; removed: number }>(`/library/${kind}`, { method: 'DELETE' }),
  comment:  (id: string, body: string) => j<any>(`/tickets/${id}/comments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body, author: 'human' }),
  }),
  runParallel: (id: string) =>
    j<{ started: boolean; subtasks: number }>(`/tickets/${id}/run-parallel`, { method: 'POST' }),
  memoryStats:  () => j<any>('/memory/stats'),
  memorySearch: (q: string, role = 'planner', topK = 12) =>
    j<{
      query: string; used_sources: string[];
      groups: { vector: any[]; md: any[]; other: any[] };
      hits: any[];
    }>(`/memory/search?q=${encodeURIComponent(q)}&role=${role}&top_k=${topK}`),
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
  memoryCompactAll: () => j<any>('/memory/compact-all', { method: 'POST' }),
  memoryCompactAllStatus: () => j<{ running: boolean; done: boolean;
    current: string | null;
    sub: { done: number; total: number; key: string } | null;
    steps_done: string[]; total_steps: number;
    error: string | null; elapsed_s: number; result: any }>(
    '/memory/compact-all/status'),
  memoryOkr: () => j<{ ok: boolean; counts: Record<string, number>;
    active_kr: string | null; nodes: any[] }>('/memory/okf'),
  memoryOkrSetActive: (active_kr: string | null) =>
    j<any>('/memory/okf/active', { method: 'POST', body: JSON.stringify({ active_kr }) }),
  memoryOkrMigrate: () => j<{ ok: boolean; migrated: number; topics: number }>(
    '/memory/okf/migrate', { method: 'POST' }),
  memoryFilesCleanup: (dry_run?: boolean) =>
    j<{ ok: boolean; folded?: number; facts?: number; stale?: string[]; count?: number }>(
      `/memory/files/cleanup${dry_run ? '?dry_run=true' : ''}`, { method: 'POST' }),
  memoryFileDelete: (name: string) =>
    j<any>(`/memory/files/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  memoryValidatePath: (location: string) =>
    j<{ ok: boolean; resolved: string; exists: boolean; is_dir: boolean;
        code_files: number; doc_files: number; sample: string[]; message: string }>(
      '/memory/validate-path', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ location }),
      }),
  memorySources: () => j<MemorySource[]>('/memory/sources'),
  memorySourceCreate: (body: { kind: string; location: string; name?: string }) =>
    j<MemorySource>('/memory/sources', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  memorySourceDelete: (id: number) =>
    apiFetch(`/memory/sources/${id}`, { method: 'DELETE' }).then(r => {
      if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
    }),
  memorySourceIndex: (id: number) =>
    j<MemorySource>(`/memory/sources/${id}/index`, { method: 'POST' }),
  memorySourceUpload: async (file: File, name?: string): Promise<MemorySource> => {
    const fd = new FormData();
    fd.append('file', file);
    if (name) fd.append('name', name);
    const r = await apiFetch(`/memory/sources/upload`, { method: 'POST', body: fd });
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
