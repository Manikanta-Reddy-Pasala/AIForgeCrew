import { j, BASE, apiFetch } from './core';
import { api } from './client';

// ── Chat session types ────────────────────────────────────────────

export interface ChatSession {
  id: number;
  title: string;
  cwd: string | null;
  role?: string | null;
  created_at: string;
  updated_at: string;
  message_count?: number;
  last_mode?: 'simple' | 'plan' | 'team';   // mode the latest user turn ran in
}

export interface ChatModelEntry {
  id: string;
  label: string;
  active: boolean;
  /** The endpoint this copy of the model is registered against. Two entries can
   *  share `id` and differ only here — send it back when picking. */
  base_url?: string;
}

export interface ChatModelsResponse {
  provider: string;
  current: string | null;
  /** Endpoint of the currently selected copy — pairs with `current`. */
  current_base_url?: string;
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

export interface ChatTraceAction {
  type: 'tool' | 'thought' | 'error' | 'plan_ready';
  name?: string;
  args?: any;
  result?: any;
  text?: string;
}

export interface ChatTraceTurn {
  ts: string;
  mode: 'simple' | 'team';
  cwd?: string;
  prompt: string;
  n_tools: number;
  actions: ChatTraceAction[];
  response: string;
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
  autoAssign: api.autoAssign,
  syncModels: api.syncModels,
  approvalSettings: api.approvalSettings,
  setApprovalMode: api.setApprovalMode,
  providersTest: api.providersTest,
  testNative: api.testNative,
  // AgentSettings reads the per-role config through chatApi too; without this
  // delegation the call is undefined, throws, and the swallowed catch leaves
  // every agent dropdown stuck on "— pick a model —" despite saved config.
  agentsV2Config: api.agentsV2Config,

  chatModels: () => j<ChatModelsResponse>('/chat/models'),

  // base_url says WHICH copy of the model: the same id can be registered
  // against two servers, and without it the backend can only guess — which is
  // how a model added from a second host kept being called on the first.
  setChatModel: (model: string, provider?: string, baseUrl?: string) =>
    j<{ provider: string; model: string; active: boolean }>('/chat/model', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, ...(provider ? { provider } : {}),
                             ...(baseUrl ? { base_url: baseUrl } : {}) }),
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

  // Per-turn action+response trace (from ~/.aiforge/chat_traces) for review.
  sessionTrace: (id: number) =>
    j<{ session_id: number; count: number; turns: ChatTraceTurn[] }>(
      `/chat/sessions/${id}/trace`),

  sessionRename: (id: number, title: string) =>
    j<ChatSession>(`/chat/sessions/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    }),

  sessionDelete: (id: number) =>
    apiFetch(`/chat/sessions/${id}`, { method: 'DELETE' }).then(r => {
      if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
    }),

  // Resolve a pending approval gate (#1) — Approve/Reject a risky action.
  // `note` = optional guidance the user typed on the card; on reject it steers
  // the agent to adjust and continue instead of just stopping.
  approve: (id: number, decision: 'approve' | 'reject', seq?: number, note?: string) =>
    j<{ resolved: boolean; decision: string }>(`/chat/sessions/${id}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, ...(seq != null ? { id: seq } : {}),
                             ...(note ? { note } : {}) }),
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
        body: JSON.stringify({ sha, ...opts }),
      }),
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
  return apiFetch(`/chat/sessions/${sessionId}/media`, { method: 'POST', body: fd })
    .then(async r => { if (!r.ok) { throw new Error((await r.json().catch(() => ({}))).detail || `upload failed (${r.status})`); } return r.json(); });
}
export function chatMediaList(sessionId: number): Promise<{ media: ChatMedia[]; vision: boolean }> {
  return j(`/chat/sessions/${sessionId}/media`);
}
export function chatSessionSpec(sessionId: number): Promise<{ exists: boolean; path?: string; content?: string }> {
  return j(`/chat/sessions/${sessionId}/spec`);
}
export function chatMediaDescribe(mediaId: number, description: string): Promise<ChatMedia> {
  return j(`/chat/media/${mediaId}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ description }),
  });
}
export function chatMediaDelete(mediaId: number): Promise<void> {
  return apiFetch(`/chat/media/${mediaId}`, { method: 'DELETE' }).then(r => {
    if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
  });
}
export function chatMediaRawURL(mediaId: number): string {
  return `${BASE}/chat/media/${mediaId}/raw`;
}

// Stop the in-flight run for a session (halts agents + kills subprocesses).
export function chatSessionStop(id: number): Promise<{ stopped: boolean }> {
  return apiFetch(`/chat/sessions/${id}/stop`, { method: 'POST' })
    .then(r => r.ok ? r.json() : { stopped: false })
    .catch(() => ({ stopped: false }));
}

// Force-reset ALL chat runs (kill all) — recovers from a wedged run that left a
// session stuck busy or a new chat waiting on the team run lock.
export function chatKillAll(): Promise<{ killed: number[]; count: number; team_lock_released: boolean }> {
  return apiFetch(`/chat/kill-all`, { method: 'POST' })
    .then(r => r.ok ? r.json() : { killed: [], count: 0, team_lock_released: false })
    .catch(() => ({ killed: [], count: 0, team_lock_released: false }));
}

// Steer the IN-FLIGHT run without stopping it (Gap A — mid-run steering).
// The message is queued and folded into the agent's context at its next step.
export function chatSessionSteer(id: number, content: string): Promise<{ queued: boolean; unsupported?: boolean; reason?: string }> {
  return apiFetch(`/chat/sessions/${id}/steer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
    .then(r => r.ok ? r.json() : { queued: false })
    .catch(() => ({ queued: false }));
}
