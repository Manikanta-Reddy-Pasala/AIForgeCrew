import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { chatApi, chatSessionMessageURL, chatSessionStop, ChatSession, ChatMsg, ChatModelEntry } from '../api';
import { Icon } from '../icons';
import { MdLite } from '../mdlite';

// ── types ──────────────────────────────────────────────────────────────────────

type AgentStep =
  | { kind: 'thought'; text: string; role?: string }
  | { kind: 'tool'; name: string; args: object; result: object; role?: string }
  | { kind: 'error'; text: string; role?: string };

// A "live" turn: the in-progress assistant turn while streaming.
type LiveTurn = {
  role: 'assistant';
  text: string;
  steps: AgentStep[];
  streaming: boolean;
  elapsedSec?: number;
  awaiting?: boolean;   // agent asked a question — waiting for your reply
};

// ── Elapsed time formatter ────────────────────────────────────────────────────

function fmtElapsed(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

const LS_SESSION_KEY = 'aiforge.chat.activeSessionId';
const LS_MODEL_KEY = 'aiforge.chat.model';
const LS_MODE_KEY = 'aiforge.chat.flowmode';

type ChatMode = 'simple' | 'plan' | 'team';

// A pending human-approval gate (#1): the run is blocked until the user
// Approves/Rejects this action.
type PendingApproval = {
  id: number;          // seq echoed back to the server
  name: string;
  args: object;
  reason?: string;
  preview?: string;
};

// ── relative time helper ──────────────────────────────────────────────────────

function relTime(isoStr: string): string {
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

// ── Convert a persisted ChatMsg step (from server) to AgentStep ───────────────

function toAgentStep(raw: any): AgentStep | null {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.type === 'thought' || raw.kind === 'thought') {
    return { kind: 'thought', text: raw.text || '', role: raw.role };
  }
  if (raw.type === 'tool' || raw.kind === 'tool') {
    return { kind: 'tool', name: raw.name || '', args: raw.args || {}, result: raw.result || {}, role: raw.role };
  }
  if (raw.type === 'error' || raw.kind === 'error') {
    return { kind: 'error', text: raw.text || '' };
  }
  return null;
}

// ── Chat component ─────────────────────────────────────────────────────────────

export default function Chat() {
  // Sessions list
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);

  // Active session
  const [activeId, setActiveId] = useState<number | null>(() => {
    try {
      const v = localStorage.getItem(LS_SESSION_KEY);
      return v ? Number(v) : null;
    } catch { return null; }
  });
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [msgsLoading, setMsgsLoading] = useState(false);

  // Live turn (the assistant response being streamed right now)
  const [liveTurn, setLiveTurn] = useState<LiveTurn | null>(null);

  // Composer
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);

  // Rename state: { id, value }
  const [renaming, setRenaming] = useState<{ id: number; value: string } | null>(null);

  // Chat mode: 'simple' (single agent) | 'team' (full ADK pipeline)
  const [chatMode, setChatMode] = useState<ChatMode>(() => {
    try {
      const v = localStorage.getItem(LS_MODE_KEY);
      return (v === 'team' || v === 'plan' ? v : 'simple') as ChatMode;
    } catch { return 'simple'; }
  });

  // Pending approval gate (#1) + checkpoints panel (#3).
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [checkpoints, setCheckpoints] = useState<Array<{ sha: string; label: string; when: string }> | null>(null);

  // Model selector
  const [modelOptions, setModelOptions] = useState<ChatModelEntry[]>([]);
  const [chatProvider, setChatProvider] = useState<string>('');
  const [selectedModel, setSelectedModel] = useState<string>(() => {
    try { return localStorage.getItem(LS_MODEL_KEY) || ''; } catch { return ''; }
  });
  const [modelActive, setModelActive] = useState<boolean>(true);

  // Elapsed timer for the live streaming turn
  const [elapsedSec, setElapsedSec] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const sendStartRef = useRef<number>(0);

  const logRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const renameInputRef = useRef<HTMLInputElement | null>(null);
  // Aborts the in-flight chat stream (Stop button + cleanup on
  // unmount / session switch so a half-streamed turn doesn't leak).
  const abortRef = useRef<AbortController | null>(null);

  function stopRun() {
    // Tell the server to halt the run (agents + sub-agents + subprocesses)
    // FIRST, then drop the client stream.
    if (activeId !== null) chatSessionStop(activeId);
    abortRef.current?.abort();
    abortRef.current = null;
    if (timerRef.current !== null) { clearInterval(timerRef.current); timerRef.current = null; }
    setBusy(false);
    setLiveTurn(prev => prev ? { ...prev, streaming: false } : null);
    toast('Stopping run…');
    // Pull whatever the server persisted once it unwinds.
    if (activeId !== null) setTimeout(() => loadSession(activeId), 800);
  }

  // ── Approval gate (#1) ──────────────────────────────────────────────────────
  async function resolveApproval(decision: 'approve' | 'reject') {
    const p = pendingApproval;
    if (!p || activeId === null) return;
    setPendingApproval(null);   // optimistic — run resumes server-side
    try {
      await chatApi.approve(activeId, decision, p.id);
    } catch (e: any) {
      toast.error(`Approval failed: ${e.message}`);
    }
  }

  // ── Checkpoints (#3) ────────────────────────────────────────────────────────
  async function openCheckpoints() {
    if (activeId === null) return;
    try {
      const res = await chatApi.checkpoints(activeId);
      setCheckpoints(res.checkpoints || []);
    } catch (e: any) {
      toast.error(`Couldn't load checkpoints: ${e.message}`);
    }
  }

  async function restoreCheckpoint(sha: string) {
    if (activeId === null) return;
    if (!window.confirm('Restore the workspace to this checkpoint? Tracked files revert to the snapshot; files created after it are left in place.')) return;
    try {
      const res = await chatApi.checkpointRestore(activeId, sha);
      if (res.ok) {
        toast.success(`Restored${res.left_in_place && res.left_in_place.length ? ` (${res.left_in_place.length} newer file(s) left in place)` : ''}`);
        setCheckpoints(null);
      } else {
        toast.error(`Restore failed: ${res.error || 'unknown'}`);
      }
    } catch (e: any) {
      toast.error(`Restore failed: ${e.message}`);
    }
  }

  // ── Load sessions list ─────────────────────────────────────────────────────

  async function loadSessions(silent = false) {
    if (!silent) setSessionsLoading(true);
    try {
      const list = await chatApi.sessions();
      setSessions(list);
    } catch (e: any) {
      if (!silent) toast.error(`Failed to load sessions: ${e.message}`);
    } finally {
      if (!silent) setSessionsLoading(false);
    }
  }

  // ── Load a specific session's messages ────────────────────────────────────

  async function loadSession(id: number) {
    setMsgsLoading(true);
    setMessages([]);
    setLiveTurn(null);
    try {
      const res = await chatApi.sessionGet(id);
      setMessages(res.messages);
    } catch (e: any) {
      toast.error(`Failed to load session: ${e.message}`);
      // Session may have been deleted; clear active
      setActiveId(null);
      try { localStorage.removeItem(LS_SESSION_KEY); } catch { /* ignore */ }
    } finally {
      setMsgsLoading(false);
    }
  }

  // ── On mount: load sessions + models; if active ID persisted, load it too ──

  useEffect(() => {
    // Fetch available chat model options (non-blocking; failures are silent)
    chatApi.chatModels().then(async resp => {
      setChatProvider(resp.provider || '');
      const models = resp.models || [];
      setModelOptions(models);

      // If saved selection is not active AND there is at least one active model, auto-switch
      if (resp.current_active === false && models.length > 0) {
        const firstActive = models[0];
        setSelectedModel(firstActive.id);
        setModelActive(firstActive.active);
        try {
          await chatApi.setChatModel(firstActive.id, resp.provider || undefined);
        } catch { /* ignore — best-effort */ }
        toast.warning(`Previous chat model not loaded — switched to ${firstActive.label || firstActive.id}`);
      } else {
        // Use backend's current as ground truth; fall back to localStorage, then first option
        setSelectedModel(prev => {
          const backendCurrent = resp.current || '';
          const localPersisted = prev;
          const allIds = models.map((m: ChatModelEntry) => m.id);
          if (backendCurrent && allIds.includes(backendCurrent)) return backendCurrent;
          if (localPersisted && allIds.includes(localPersisted)) return localPersisted;
          return backendCurrent || (models[0]?.id ?? '');
        });
        setModelActive(resp.current_active ?? true);
      }
    }).catch(() => { /* backend may not have the endpoint yet — ignore */ });

    loadSessions().then(() => {
      // sessions loaded; if there's an activeId, load it
      if (activeId !== null) {
        loadSession(activeId);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Persist active session id
  useEffect(() => {
    try {
      if (activeId !== null) {
        localStorage.setItem(LS_SESSION_KEY, String(activeId));
      } else {
        localStorage.removeItem(LS_SESSION_KEY);
      }
    } catch { /* ignore */ }
  }, [activeId]);

  // Persist selected model
  useEffect(() => {
    try { if (selectedModel) localStorage.setItem(LS_MODEL_KEY, selectedModel); } catch { /* ignore */ }
  }, [selectedModel]);

  // Persist chat mode
  useEffect(() => {
    try { localStorage.setItem(LS_MODE_KEY, chatMode); } catch { /* ignore */ }
  }, [chatMode]);

  // Auto-scroll on new messages / live turn updates
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, liveTurn]);

  // Focus rename input when rename starts
  useEffect(() => {
    if (renaming) {
      setTimeout(() => renameInputRef.current?.focus(), 30);
    }
  }, [renaming]);

  // Cleanup timer interval on unmount to prevent leaks
  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  // Abort the in-flight stream when leaving the Chat view (navigate away).
  useEffect(() => () => { abortRef.current?.abort(); }, []);

  // ── Select a session ──────────────────────────────────────────────────────

  function selectSession(id: number) {
    if (id === activeId) return;
    abortRef.current?.abort();   // drop any in-flight stream on the old session
    abortRef.current = null;
    setActiveId(id);
    setLiveTurn(null);
    setBusy(false);
    loadSession(id);
  }

  // ── Create a new session ──────────────────────────────────────────────────

  async function createSession(): Promise<number | null> {
    try {
      const session = await chatApi.sessionCreate();
      setSessions(prev => [session, ...prev]);
      setActiveId(session.id);
      setMessages([]);
      setLiveTurn(null);
      return session.id;
    } catch (e: any) {
      toast.error(`Failed to create session: ${e.message}`);
      return null;
    }
  }

  async function handleNewChat() {
    await createSession();
    setTimeout(() => textareaRef.current?.focus(), 50);
  }

  // ── Delete a session ──────────────────────────────────────────────────────

  async function deleteSession(id: number) {
    if (!window.confirm('Delete this conversation?')) return;
    try {
      await chatApi.sessionDelete(id);
      setSessions(prev => prev.filter(s => s.id !== id));
      if (activeId === id) {
        setActiveId(null);
        setMessages([]);
        setLiveTurn(null);
      }
    } catch (e: any) {
      toast.error(`Delete failed: ${e.message}`);
    }
  }

  // ── Rename a session ──────────────────────────────────────────────────────

  function startRename(session: ChatSession, e: React.MouseEvent) {
    e.stopPropagation();
    setRenaming({ id: session.id, value: session.title });
  }

  async function commitRename() {
    if (!renaming) return;
    const trimmed = renaming.value.trim();
    setRenaming(null);
    if (!trimmed) return;
    // Find current title to check if changed
    const current = sessions.find(s => s.id === renaming.id);
    if (current && current.title === trimmed) return;
    try {
      const updated = await chatApi.sessionRename(renaming.id, trimmed);
      setSessions(prev => prev.map(s => s.id === renaming.id ? { ...s, title: updated.title } : s));
    } catch (e: any) {
      toast.error(`Rename failed: ${e.message}`);
    }
  }

  function onRenameKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') { e.preventDefault(); commitRename(); }
    if (e.key === 'Escape') { setRenaming(null); }
  }

  // ── SSE streaming send ────────────────────────────────────────────────────

  async function send() {
    const q = input.trim();
    if (!q || busy) return;
    setInput('');

    // Ensure we have a session
    let sessionId = activeId;
    if (sessionId === null) {
      const newId = await createSession();
      if (newId === null) return;
      sessionId = newId;
    }

    // Optimistically append user message
    const optimisticUser: ChatMsg = {
      id: -(Date.now()), // placeholder, negative to distinguish
      role: 'user',
      content: q,
      steps: [],
      created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, optimisticUser]);

    // Start live assistant turn
    const initialLive: LiveTurn = { role: 'assistant', text: '', steps: [], streaming: true };
    setLiveTurn(initialLive);
    setBusy(true);

    // Start elapsed timer
    sendStartRef.current = Date.now();
    setElapsedSec(0);
    if (timerRef.current !== null) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - sendStartRef.current) / 1000));
    }, 1000);

    const isFirstMessage = messages.length === 0;

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await fetch(chatSessionMessageURL(sessionId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: q, mode: chatMode }),
        signal: ctrl.signal,
      });

      if (!res.ok) {
        let detail = '';
        try { const b = await res.json(); detail = b?.detail || b?.error || ''; } catch { /* ignore */ }
        try { if (!detail) detail = await res.text(); } catch { /* ignore */ }
        throw new Error(`${res.status} ${res.statusText}${detail ? ` — ${detail}` : ''}`);
      }

      const reader = res.body!.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      function applyEvent(raw: string) {
        const line = raw.startsWith('data: ') ? raw.slice(6) : raw;
        if (!line.trim()) return;
        let evt: any;
        try { evt = JSON.parse(line); } catch { return; }

        // Approval gate (#1): the run is blocked server-side; surface the
        // action + diff so the user can Approve/Reject. Cleared when the
        // next tool/message event arrives (the run resumed).
        if (evt.type === 'approval') {
          setPendingApproval({ id: evt.id, name: evt.name, args: evt.args || {}, reason: evt.reason, preview: evt.preview });
          return;
        }
        if (evt.type === 'tool' || evt.type === 'message') setPendingApproval(null);

        setLiveTurn(prev => {
          if (!prev) return prev;
          if (evt.type === 'thought') {
            return { ...prev, steps: [...prev.steps, { kind: 'thought' as const, text: evt.text, role: evt.role }] };
          }
          if (evt.type === 'tool') {
            return { ...prev, steps: [...prev.steps, { kind: 'tool' as const, name: evt.name, args: evt.args || {}, result: evt.result || {}, role: evt.role }] };
          }
          if (evt.type === 'message') {
            if (evt.awaiting_input) setTimeout(() => textareaRef.current?.focus(), 30);
            return { ...prev, text: evt.text, streaming: false, awaiting: !!evt.awaiting_input };
          }
          if (evt.type === 'error') {
            return { ...prev, text: evt.text, steps: [...prev.steps, { kind: 'error' as const, text: evt.text }], streaming: false };
          }
          if (evt.type === 'done') {
            return { ...prev, streaming: false };
          }
          return prev;
        });
      }

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop() ?? '';
        for (const part of parts) {
          if (part.trim()) {
            for (const line of part.split('\n')) {
              if (line.startsWith('data: ')) applyEvent(line);
            }
          }
        }
      }

      // Flush remaining buffer
      if (buffer.trim()) {
        for (const line of buffer.split('\n')) {
          if (line.startsWith('data: ')) applyEvent(line);
        }
      }

      // Ensure streaming is cleared; freeze the elapsed timer
      if (timerRef.current !== null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      const finalElapsed = Math.floor((Date.now() - sendStartRef.current) / 1000);
      setElapsedSec(finalElapsed);
      setLiveTurn(prev => prev ? { ...prev, streaming: false, elapsedSec: finalElapsed } : null);

      // Re-fetch session messages to get persisted IDs and auto-title
      await loadSession(sessionId);
      setLiveTurn(null);

      // Refresh sidebar (for auto-title on first message, and updated_at)
      if (isFirstMessage) {
        await loadSessions(true);
      } else {
        // Silently update the session's updated_at in the list
        loadSessions(true);
      }

    } catch (e: any) {
      if (timerRef.current !== null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      // User pressed Stop (or navigated away) — not an error.
      if (e?.name === 'AbortError') {
        setLiveTurn(prev => prev ? { ...prev, streaming: false } : null);
      } else {
        const finalElapsed = Math.floor((Date.now() - sendStartRef.current) / 1000);
        setElapsedSec(finalElapsed);
        setLiveTurn(prev => prev ? { ...prev, text: `Agent error: ${e.message}`, streaming: false, elapsedSec: finalElapsed } : null);
        toast.error(`Agent failed: ${e.message}`);
      }
    } finally {
      abortRef.current = null;
      setBusy(false);
      textareaRef.current?.focus();
    }

  }

  function onKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  const activeSession = sessions.find(s => s.id === activeId) || null;

  return (
    <div className="chat-shell-v2">
      {/* ── Left sidebar: sessions list ─────────────────────────────────────── */}
      <div className="chat-sessions-sidebar">
        <div className="chat-sessions-header">
          <button onClick={handleNewChat} disabled={busy}>
            <Icon.Plus size={13} /> New chat
          </button>
        </div>
        <div className="chat-sessions-list">
          {sessionsLoading ? (
            <div style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {[1,2,3].map(i => (
                <div key={i} className="skeleton" style={{ height: 44, borderRadius: 8 }} />
              ))}
            </div>
          ) : sessions.length === 0 ? (
            <div style={{ padding: '16px 10px', textAlign: 'center', color: 'var(--fg-3)', fontSize: 'var(--fs-xs)' }}>
              No conversations yet
            </div>
          ) : sessions.map(s => (
            <div
              key={s.id}
              className={`chat-session-item ${s.id === activeId ? 'active' : ''}`}
              onClick={() => { if (!renaming || renaming.id !== s.id) selectSession(s.id); }}
            >
              <div className="chat-session-item-body">
                {renaming && renaming.id === s.id ? (
                  <input
                    ref={renameInputRef}
                    className="chat-session-rename-input"
                    value={renaming.value}
                    onChange={e => setRenaming(r => r ? { ...r, value: e.target.value } : r)}
                    onKeyDown={onRenameKey}
                    onBlur={commitRename}
                    onClick={e => e.stopPropagation()}
                  />
                ) : (
                  <>
                    <div className="chat-session-title" title={s.title}>{s.title || 'Untitled'}</div>
                    <div className="chat-session-meta">
                      {relTime(s.updated_at)}
                      {s.message_count != null && s.message_count > 0
                        ? ` · ${s.message_count} msg${s.message_count === 1 ? '' : 's'}`
                        : ''}
                    </div>
                  </>
                )}
              </div>
              {(!renaming || renaming.id !== s.id) && (
                <div className="chat-session-actions">
                  <button
                    title="Rename"
                    onClick={e => startRename(s, e)}
                  >
                    ✎
                  </button>
                  <button
                    title="Delete"
                    onClick={e => { e.stopPropagation(); deleteSession(s.id); }}
                  >
                    <Icon.X size={11} />
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* ── Main pane ──────────────────────────────────────────────────────────── */}
      <div className="chat-v2-main">
        {/* Topbar */}
        <div className="chat-topbar">
          <div className="row">
            <span className="topbar-title" style={{ fontSize: 'var(--fs-md)' }}>
              {activeSession ? activeSession.title || 'Untitled' : 'Agent Chat'}
            </span>
            {activeSession && (
              <span className="xs muted" style={{ fontFamily: 'var(--font-mono)' }}>
                reads & writes files · runs commands
              </span>
            )}
          </div>
          <div className="row" style={{ gap: 'var(--s-2)' }}>
            {/* Simple | Plan | Team mode toggle */}
            <div className="chat-mode-toggle" title="Simple: single agent · Plan: read-only, proposes a plan first · Team: full ADK planner→doer→learner pipeline">
              <button
                className={chatMode === 'simple' ? 'active' : ''}
                onClick={() => setChatMode('simple')}
                disabled={busy}
              >
                Simple
              </button>
              <button
                className={chatMode === 'plan' ? 'active' : ''}
                onClick={() => setChatMode('plan')}
                disabled={busy}
                title="Read-only: the agent inspects the repo and proposes a plan; switch to Simple/Team to execute"
              >
                Plan
              </button>
              <button
                className={chatMode === 'team' ? 'active' : ''}
                onClick={() => setChatMode('team')}
                disabled={busy}
              >
                Team (full flow)
              </button>
            </div>

            {/* Model selector — less relevant in team mode, so hide it */}
            {chatMode !== 'team' && (
              <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 'var(--fs-xs)', color: 'var(--fg-2)' }}>
                <span
                  title={modelActive ? 'Model is loaded and active' : 'Model is not currently loaded'}
                  style={{
                    display: 'inline-block',
                    width: 8,
                    height: 8,
                    borderRadius: '50%',
                    background: modelActive ? 'var(--ok, #22c55e)' : 'var(--warn, #f59e0b)',
                    flexShrink: 0,
                  }}
                />
                Model
                <select
                  className="chat-model-select"
                  value={selectedModel}
                  onChange={async e => {
                    const newModel = e.target.value;
                    setSelectedModel(newModel);
                    try {
                      const res = await chatApi.setChatModel(newModel, chatProvider || undefined);
                      setModelActive(res.active);
                      if (res.active) {
                        toast.success('Model updated');
                      } else {
                        const label = modelOptions.find(o => o.id === newModel)?.label || newModel;
                        toast.warning(`${label} is not loaded — it may fail or take time to load`);
                      }
                    } catch (err: any) {
                      toast.error(`Failed to set model: ${err.message}`);
                    }
                  }}
                  disabled={busy || modelOptions.length === 0}
                  title="Chat model"
                >
                  {modelOptions.length === 0 ? (
                    <option value="" disabled>
                      no active models — load one in LM Studio / configure on Home
                    </option>
                  ) : (
                    modelOptions.map(opt => (
                      <option key={opt.id} value={opt.id}>
                        {opt.label}
                      </option>
                    ))
                  )}
                </select>
              </label>
            )}

            {activeSession && (
              <>
                <button
                  className="ghost sm"
                  onClick={openCheckpoints}
                  title="Workspace checkpoints — roll back this session's edits"
                >
                  ↶ Checkpoints
                </button>
                <button
                  className="ghost sm"
                  onClick={() => setRenaming({ id: activeSession.id, value: activeSession.title })}
                  title="Rename this conversation"
                >
                  Rename
                </button>
                <button
                  className="ghost sm"
                  style={{ color: 'var(--err)' }}
                  onClick={() => deleteSession(activeSession.id)}
                >
                  Delete
                </button>
              </>
            )}
          </div>
        </div>

        {/* Message log or empty state */}
        {activeId === null ? (
          <div className="chat-empty-state">
            <div className="empty-icon">💬</div>
            <h3>Start a conversation</h3>
            <p>Click <strong>New chat</strong> to begin, or select a past conversation from the sidebar.</p>
            <button onClick={handleNewChat}>
              <Icon.Plus size={14} /> New chat
            </button>
          </div>
        ) : msgsLoading ? (
          <div className="chat-log" style={{ justifyContent: 'center', alignItems: 'center' }}>
            <div className="typing"><span /><span /><span /></div>
          </div>
        ) : (
          <>
            <div className="chat-log" ref={logRef}>
              {messages.length === 0 && !liveTurn && !busy && (
                <div className="chat-empty-state" style={{ flex: 'none' }}>
                  <div className="empty-icon">✨</div>
                  <p style={{ maxWidth: 320 }}>Send a message to get started. The agent can read and write files, run commands, and implement features.</p>
                </div>
              )}

              {/* Persisted messages */}
              {messages.map(msg => (
                <div
                  key={msg.id}
                  className={`bubble ${msg.role === 'user' ? 'user' : 'sys'}`}
                >
                  <div className="bubble-avatar">{msg.role === 'user' ? 'You' : 'AI'}</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {msg.role === 'assistant' ? (
                      <AssistantBubble
                        text={msg.content}
                        steps={(msg.steps || []).map(toAgentStep).filter((s): s is AgentStep => s !== null)}
                        streaming={false}
                      />
                    ) : (
                      <div className="bubble-body">
                        <span style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</span>
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {/* Live streaming assistant turn */}
              {liveTurn && (
                <div className="bubble sys">
                  <div className="bubble-avatar">AI</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <AssistantBubble
                      text={liveTurn.text}
                      steps={liveTurn.steps}
                      streaming={liveTurn.streaming}
                      elapsedSec={liveTurn.streaming ? elapsedSec : liveTurn.elapsedSec}
                    />
                    {liveTurn.awaiting && (
                      <div style={{
                        marginTop: 6, fontSize: 12, fontWeight: 600,
                        color: 'var(--accent, #2563eb)',
                      }}>
                        ❓ Waiting for your reply — answer below to continue.
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Approval gate (#1): run is paused, awaiting Approve/Reject */}
              {pendingApproval && (
                <div style={{
                  margin: '8px 0', padding: 12,
                  border: '1px solid var(--warn, #f59e0b)',
                  borderRadius: 8, background: 'var(--bg-1)',
                }}>
                  <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>
                    ⚠ Approval needed — <code>{pendingApproval.name}</code>
                  </div>
                  {pendingApproval.reason && (
                    <div className="muted xs" style={{ marginBottom: 6 }}>{pendingApproval.reason}</div>
                  )}
                  {pendingApproval.preview && (
                    <pre style={{
                      maxHeight: 220, overflow: 'auto', fontSize: 11,
                      background: 'var(--bg-2)', padding: 8, borderRadius: 6,
                      whiteSpace: 'pre-wrap', margin: '0 0 8px',
                    }}>{pendingApproval.preview}</pre>
                  )}
                  <div style={{ display: 'flex', gap: 8 }}>
                    <button onClick={() => resolveApproval('approve')}>✓ Approve</button>
                    <button className="danger" onClick={() => resolveApproval('reject')}>✗ Reject</button>
                  </div>
                </div>
              )}
            </div>

            <div className="chat-composer">
              <div style={{ display: 'flex', gap: 6 }}>
                <textarea
                  ref={textareaRef}
                  rows={4}
                  placeholder="Ask the agent to read/write files, run commands, implement a feature…  (Enter to send, Shift+Enter for newline)"
                  value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={onKey}
                  disabled={busy}
                  style={{ flex: 1 }}
                />
                {busy && (
                  <button onClick={stopRun} className="danger"
                          title="Stop all agents + processes for this run"
                          style={{ whiteSpace: 'nowrap' }}>
                    ■ Stop
                  </button>
                )}
                <button onClick={send} disabled={busy || !input.trim()}>
                  <Icon.Agents size={14} /> {busy ? 'Running…' : 'Run'}
                </button>
              </div>
            </div>
          </>
        )}

        {/* Composer shown even when no session: send will create one */}
        {activeId === null && (
          <div className="chat-composer">
            <div style={{ display: 'flex', gap: 6 }}>
              <textarea
                ref={textareaRef}
                rows={4}
                placeholder="Type a message to start a new conversation…  (Enter to send, Shift+Enter for newline)"
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={onKey}
                disabled={busy}
                style={{ flex: 1, minHeight: 96, resize: 'vertical',
                         fontSize: 14, lineHeight: 1.5, padding: 10 }}
              />
              {busy && (
                <button onClick={stopRun} className="danger"
                        title="Stop all agents + processes for this run"
                        style={{ whiteSpace: 'nowrap' }}>
                  ■ Stop
                </button>
              )}
              <button onClick={send} disabled={busy || !input.trim()}>
                <Icon.Agents size={14} /> Run
              </button>
            </div>
          </div>
        )}

        {/* Checkpoints panel (#3) */}
        {checkpoints !== null && (
          <div
            onClick={() => setCheckpoints(null)}
            style={{
              position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50,
            }}
          >
            <div onClick={e => e.stopPropagation()} style={{
              width: 'min(560px, 92vw)', maxHeight: '70vh', overflow: 'auto',
              background: 'var(--bg-0)', border: '1px solid var(--border-1)',
              borderRadius: 10, padding: 16,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                <strong>Workspace checkpoints</strong>
                <button className="ghost sm" onClick={() => setCheckpoints(null)}><Icon.X size={12} /></button>
              </div>
              {checkpoints.length === 0 ? (
                <div className="muted xs">No checkpoints yet. A snapshot is taken automatically before each turn (in a git repo).</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {checkpoints.map(c => (
                    <div key={c.sha} style={{
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                      gap: 8, padding: 8, border: '1px solid var(--border-0)', borderRadius: 6,
                    }}>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.label}</div>
                        <div className="muted xs" style={{ fontFamily: 'var(--font-mono)' }}>{c.when} · {c.sha.slice(0, 8)}</div>
                      </div>
                      <button className="ghost sm" onClick={() => restoreCheckpoint(c.sha)} title="Restore the workspace to this snapshot">↶ Restore</button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── AssistantBubble — renders steps + final text ──────────────────────────────

function AssistantBubble({
  text,
  steps,
  streaming,
  elapsedSec,
}: {
  text: string;
  steps: AgentStep[];
  streaming: boolean;
  elapsedSec?: number;
}) {
  return (
    <div>
      {steps.length > 0 && (
        <div style={{ marginBottom: text ? 8 : 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {steps.map((s, i) => (
            <AgentStepRow key={i} step={s} />
          ))}
        </div>
      )}
      {text && (
        <div className="bubble-body">
          <MdLite text={text} />
        </div>
      )}
      {streaming && !text && steps.length === 0 && (
        <div className="bubble-body" style={{ padding: 0, background: 'transparent', border: 0 }}>
          <div className="typing"><span /><span /><span /></div>
        </div>
      )}
      {streaming && (steps.length > 0 || text) && (
        <div style={{ marginTop: 4, padding: '0 2px' }}>
          <div className="typing" style={{ padding: '4px 0' }}><span /><span /><span /></div>
        </div>
      )}
      {elapsedSec !== undefined && (
        <div style={{
          marginTop: 6,
          fontSize: 'var(--fs-xs)',
          color: 'var(--fg-3)',
          display: 'flex',
          alignItems: 'center',
          gap: 4,
          fontVariantNumeric: 'tabular-nums',
        }}>
          {streaming ? (
            <span>⏱ {fmtElapsed(elapsedSec)}</span>
          ) : (
            <span className="muted xs">· {fmtElapsed(elapsedSec)}</span>
          )}
        </div>
      )}
    </div>
  );
}

// ── Agent step row ─────────────────────────────────────────────────────────────

// Small pill showing WHICH agent produced a step (team mode). Stable
// color per role name so the eye can track each agent across steps.
function AgentBadge({ role }: { role?: string }) {
  if (!role) return null;
  let h = 0;
  for (let i = 0; i < role.length; i++) h = (h * 31 + role.charCodeAt(i)) % 360;
  return (
    <span style={{
      flexShrink: 0,
      fontFamily: 'var(--font-mono)',
      fontStyle: 'normal',
      fontSize: '10px',
      fontWeight: 600,
      padding: '1px 6px',
      borderRadius: 999,
      color: `hsl(${h} 70% 30%)`,
      background: `hsl(${h} 70% 92%)`,
      border: `1px solid hsl(${h} 60% 80%)`,
      marginTop: 1,
      whiteSpace: 'nowrap',
    }} title={`agent: ${role}`}>{role}</span>
  );
}

// A thought/reasoning step. Long chain-of-thought (reasoning models dump
// their whole "Thinking Process…") is collapsed to one line so each
// agent reads as a clean structured step; click to expand the full text.
function ThoughtRow({ step }: { step: Extract<AgentStep, { kind: 'thought' }> }) {
  const long = step.text.length > 180;
  const [open, setOpen] = useState(!long);
  const preview = long && !open
    ? step.text.replace(/\s+/g, ' ').slice(0, 140) + '…'
    : step.text;
  return (
    <div style={{
      display: 'flex', gap: 6, alignItems: 'flex-start',
      padding: '5px 10px',
      background: 'var(--bg-1)',
      border: '1px solid var(--border-0)',
      borderRadius: 'var(--r-sm)',
      fontStyle: 'italic',
      color: 'var(--fg-2)',
      fontSize: 'var(--fs-xs)',
      lineHeight: 1.5,
    }}>
      <span style={{ flexShrink: 0, marginTop: 1 }}>💭</span>
      <AgentBadge role={step.role} />
      <span style={{ whiteSpace: 'pre-wrap', flex: 1 }}>{preview}</span>
      {long && (
        <button
          onClick={() => setOpen(o => !o)}
          className="ghost sm"
          style={{ flexShrink: 0, fontSize: 10, fontStyle: 'normal', padding: '0 6px' }}
        >
          {open ? 'collapse' : 'expand'}
        </button>
      )}
    </div>
  );
}

function AgentStepRow({ step }: { step: AgentStep }) {
  if (step.kind === 'thought') {
    return <ThoughtRow step={step} />;
  }
  if (step.kind === 'tool') {
    const res = step.result as any;
    const ok = res?.ok !== false && !res?.error;
    const snippet = ok
      ? (res?.output ? String(res.output).slice(0, 120) : 'ok')
      : (res?.error ? String(res.error).slice(0, 120) : 'error');
    return (
      <div style={{
        display: 'flex', gap: 6, alignItems: 'flex-start',
        padding: '5px 10px',
        background: 'var(--bg-1)',
        border: '1px solid var(--border-0)',
        borderRadius: 'var(--r-sm)',
        fontSize: 'var(--fs-xs)',
        lineHeight: 1.5,
        fontFamily: 'var(--font-mono)',
        color: ok ? 'var(--fg-1)' : 'var(--err)',
      }}>
        <span style={{ flexShrink: 0, marginTop: 1 }}>🔧</span>
        <AgentBadge role={step.role} />
        <span>
          <strong>{step.name}</strong>
          {'('}
          {Object.entries(step.args as Record<string, unknown>).slice(0, 3).map(([k, v], i) =>
            `${i > 0 ? ', ' : ''}${k}=${JSON.stringify(v).slice(0, 40)}`
          ).join('')}
          {')'}
          {' → '}
          <span style={{ color: ok ? 'var(--ok)' : 'var(--err)' }}>
            {snippet}
          </span>
        </span>
      </div>
    );
  }
  if (step.kind === 'error') {
    return (
      <div style={{
        display: 'flex', gap: 6, alignItems: 'flex-start',
        padding: '5px 10px',
        background: 'var(--err-soft)',
        border: '1px solid transparent',
        borderRadius: 'var(--r-sm)',
        fontSize: 'var(--fs-xs)',
        lineHeight: 1.5,
        color: 'var(--err)',
      }}>
        <span style={{ flexShrink: 0, marginTop: 1 }}>✗</span>
        <span>{step.text}</span>
      </div>
    );
  }
  return null;
}
