// Minimal fetch wrapper against the FastAPI backend.
const BASE = '/api';

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, init);
  if (!r.ok) {
    // Try to surface the JSON `detail` field FastAPI returns on 4xx, so
    // the UI can show an actionable message instead of "400 Bad Request".
    let detail = '';
    try {
      const body = await r.json();
      detail = body?.detail || body?.error || '';
    } catch {
      try { detail = await r.text(); } catch { /* ignore */ }
    }
    const suffix = detail ? ` — ${detail}` : '';
    throw new Error(`${r.status} ${r.statusText}${suffix}`);
  }
  return r.json();
}

export const api = {
  health:   () => j<any>('/health'),
  agents:   () => j<any[]>('/agents'),
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
  comment:  (id: string, body: string) => j<any>(`/tickets/${id}/comments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body, author: 'human' }),
  }),
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

// ── agent v2 config types ─────────────────────────────────────────

export type AgentRole =
  | 'architect' | 'planner' | 'verifier' | 'doer' | 'feedback' | 'learner';

export type ProviderId = 'local' | 'ollama_cloud' | 'anthropic' | 'openai_compatible' | 'claude_local';
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

export interface AgentRoleConfigInput {
  provider: ProviderId;
  model: string;
  base_url?: string | null;
  api_key?: string | null;
  insecure_tls?: boolean;
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

  chatModels: () => j<ChatModelsResponse>('/chat/models'),

  setChatModel: (model: string, provider?: string) =>
    j<{ provider: string; model: string; active: boolean }>('/chat/model', {
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
};

export function chatSessionMessageURL(id: number): string {
  return `${BASE}/chat/sessions/${id}/message`;
}

// Stop the in-flight run for a session (halts agents + kills subprocesses).
export function chatSessionStop(id: number): Promise<{ stopped: boolean }> {
  return fetch(`${BASE}/chat/sessions/${id}/stop`, { method: 'POST' })
    .then(r => r.ok ? r.json() : { stopped: false })
    .catch(() => ({ stopped: false }));
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
