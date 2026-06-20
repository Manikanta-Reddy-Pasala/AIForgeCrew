import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { chatApi, chatSessionMessageURL, chatSessionTicket, traceStreamURL, ticketStatus, ticketAnswer, ChatSession, ChatMsg, ChatModelOption } from '../api';
import { Icon } from '../icons';
import { MdLite } from '../mdlite';

// ── types ──────────────────────────────────────────────────────────────────────

type AgentStep =
  | { kind: 'thought'; text: string }
  | { kind: 'tool'; name: string; args: object; result: object }
  | { kind: 'error'; text: string };

// A "live" turn: the in-progress assistant turn while streaming.
type LiveTurn = {
  role: 'assistant';
  text: string;
  steps: AgentStep[];
  streaming: boolean;
};

type ChatMode = 'agent' | 'pipeline';

// A pipeline run turn rendered in the chat log
type PipelineTurn = {
  ticketId: string;          // e.g. ONE-123
  project: string | null;
  stageLines: string[];      // formatted stage-update lines
  running: boolean;
  finalStatus: string | null;
  // Clarification / awaiting-input state
  awaitingInput: boolean;
  clarifyQuestion: string;   // question text shown to user
  answerDraft: string;       // current value of the answer <input>
  answerBusy: boolean;       // true while POST /answer is in-flight
  answersGiven: string[];    // history of answers appended to the bubble
};

const LS_SESSION_KEY = 'aiforge.chat.activeSessionId';
const LS_ROLE_KEY = 'aiforge.chat.role';
const LS_MODE_KEY = 'aiforge.chat.mode';

// Statuses that definitively end the run (blocked is handled separately — it
// may mean "awaiting input" rather than a permanent failure).
const TERMINAL_STATUSES = new Set(['done', 'qa', 'qa_failed', 'cancelled']);

// Format a generic trace event object as a human-readable stage line
function formatTraceEvent(evt: Record<string, unknown>): string {
  const role = (evt.role ?? evt.agent_role ?? '') as string;
  const kind = (evt.kind ?? '') as string;
  const stage = (evt.stage ?? '') as string;
  const status = (evt.status ?? '') as string;
  const body = (evt.body ?? evt.text ?? '') as string;

  const prefix = role ? `[${role}]` : kind ? `[${kind}]` : '';

  let detail = '';
  if (stage && status) detail = `${stage} → ${status}`;
  else if (stage) detail = stage;
  else if (status) detail = `status → ${status}`;
  else if (kind) detail = kind;

  const snippet = body ? ` · ${String(body).slice(0, 120)}` : '';
  return [prefix, detail, snippet].filter(Boolean).join(' ') || JSON.stringify(evt).slice(0, 160);
}

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
    return { kind: 'thought', text: raw.text || '' };
  }
  if (raw.type === 'tool' || raw.kind === 'tool') {
    return { kind: 'tool', name: raw.name || '', args: raw.args || {}, result: raw.result || {} };
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

  // Pipeline mode
  const [mode, setMode] = useState<ChatMode>(() => {
    try { return (localStorage.getItem(LS_MODE_KEY) as ChatMode) || 'agent'; } catch { return 'agent'; }
  });
  const [pipelineProject, setPipelineProject] = useState('');
  const [pipelineTurn, setPipelineTurn] = useState<PipelineTurn | null>(null);
  const pipelineEsRef = useRef<EventSource | null>(null);

  // Rename state: { id, value }
  const [renaming, setRenaming] = useState<{ id: number; value: string } | null>(null);

  // Model selector
  const [modelOptions, setModelOptions] = useState<ChatModelOption[]>([]);
  const [selectedRole, setSelectedRole] = useState<string>(() => {
    try { return localStorage.getItem(LS_ROLE_KEY) || 'doer'; } catch { return 'doer'; }
  });

  const logRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const renameInputRef = useRef<HTMLInputElement | null>(null);
  const answerInputRef = useRef<HTMLInputElement | null>(null);

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
      // Sync the role selector to whatever this session was using
      if (res.session.role) {
        setSelectedRole(res.session.role);
      }
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
    // Fetch available model options (non-blocking; failures are silent)
    chatApi.chatModels().then(opts => {
      setModelOptions(opts);
      // If no persisted role or it's not in the list, default to first entry
      if (opts.length > 0) {
        setSelectedRole(prev => {
          const valid = opts.some(o => o.role === prev);
          return valid ? prev : opts[0].role;
        });
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

  // Persist selected role
  useEffect(() => {
    try { localStorage.setItem(LS_ROLE_KEY, selectedRole); } catch { /* ignore */ }
  }, [selectedRole]);

  // Persist chat mode
  useEffect(() => {
    try { localStorage.setItem(LS_MODE_KEY, mode); } catch { /* ignore */ }
  }, [mode]);

  // Close pipeline EventSource on unmount
  useEffect(() => {
    return () => { pipelineEsRef.current?.close(); };
  }, []);

  // Auto-scroll on new messages / live turn updates
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, liveTurn, pipelineTurn]);

  // Focus rename input when rename starts
  useEffect(() => {
    if (renaming) {
      setTimeout(() => renameInputRef.current?.focus(), 30);
    }
  }, [renaming]);

  // ── Select a session ──────────────────────────────────────────────────────

  function selectSession(id: number) {
    if (id === activeId) return;
    // Close any active pipeline stream
    pipelineEsRef.current?.close();
    pipelineEsRef.current = null;
    setPipelineTurn(null);
    setActiveId(id);
    setLiveTurn(null);
    setBusy(false);
    loadSession(id);
  }

  // ── Create a new session ──────────────────────────────────────────────────

  async function createSession(): Promise<number | null> {
    try {
      const session = await chatApi.sessionCreate({ role: selectedRole });
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

    const isFirstMessage = messages.length === 0;

    try {
      const res = await fetch(chatSessionMessageURL(sessionId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: q, role: selectedRole }),
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

        setLiveTurn(prev => {
          if (!prev) return prev;
          if (evt.type === 'thought') {
            return { ...prev, steps: [...prev.steps, { kind: 'thought' as const, text: evt.text }] };
          }
          if (evt.type === 'tool') {
            return { ...prev, steps: [...prev.steps, { kind: 'tool' as const, name: evt.name, args: evt.args || {}, result: evt.result || {} }] };
          }
          if (evt.type === 'message') {
            return { ...prev, text: evt.text, streaming: false };
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

      // Ensure streaming is cleared
      setLiveTurn(prev => prev ? { ...prev, streaming: false } : null);

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
      setLiveTurn(prev => prev ? { ...prev, text: `Agent error: ${e.message}`, streaming: false } : null);
      toast.error(`Agent failed: ${e.message}`);
    } finally {
      setBusy(false);
      textareaRef.current?.focus();
    }

  }

  // ── Pipeline send ─────────────────────────────────────────────────────────

  /**
   * Helper: open (or re-open) an EventSource for `identifier` and stream
   * stage updates into the existing pipelineTurn. Handles:
   *  - clarification events (kind === "clarification")
   *  - blocked status — polls ticket to check awaiting_input
   *  - terminal statuses (done / qa / qa_failed / cancelled)
   */
  function openTraceStream(identifier: string) {
    // Close any previously open stream
    pipelineEsRef.current?.close();
    pipelineEsRef.current = null;

    const es = new EventSource(traceStreamURL(identifier));
    pipelineEsRef.current = es;

    es.onmessage = ev => {
      try {
        const d = JSON.parse(ev.data);

        // ── Detect clarification event ────────────────────────────────────
        const isClarifyEvent = d.kind === 'clarification';
        const isClarifyBlocked =
          (d.status === 'blocked' || d.kind === 'blocked') &&
          (d.awaiting_input === true || d.metadata?.awaiting_input === true);

        if (isClarifyEvent || isClarifyBlocked) {
          const question: string =
            d.body ?? d.text ??
            (Array.isArray(d.metadata?.questions) ? d.metadata.questions.join('\n') : '') ??
            'The pipeline needs more information to proceed.';

          es.close();
          pipelineEsRef.current = null;

          setPipelineTurn(prev => prev ? {
            ...prev,
            running: false,
            awaitingInput: true,
            clarifyQuestion: question,
            stageLines: [...prev.stageLines, `[clarification] ${question}`],
          } : null);

          // busy stays true — the user must answer before continuing
          // Focus the answer input after state settles
          setTimeout(() => answerInputRef.current?.focus(), 80);
          return;
        }

        // ── Normal event: append stage line ──────────────────────────────
        const line = formatTraceEvent(d);
        const status = (d.status ?? d.kind ?? '') as string;
        const isTerminal = TERMINAL_STATUSES.has(status);

        setPipelineTurn(prev => {
          if (!prev) return prev;
          const updated = { ...prev, stageLines: [...prev.stageLines, line] };
          if (isTerminal) {
            updated.running = false;
            updated.finalStatus = status;
          }
          return updated;
        });

        if (isTerminal) {
          es.close();
          pipelineEsRef.current = null;
          setBusy(false);
        }
      } catch { /* ignore malformed SSE frames */ }
    };

    es.onerror = () => {
      es.close();
      pipelineEsRef.current = null;

      // EventSource closes when backend closes the connection — this happens
      // on `blocked` status (backend ends the SSE stream). Poll the ticket
      // to find out if we're actually awaiting_input or truly done.
      ticketStatus(identifier).then(res => {
        const meta = res?.ticket?.metadata ?? {};
        const awaitingInput: boolean =
          meta.awaiting_input === true ||
          (Array.isArray(meta.clarify_questions) && meta.clarify_questions.length > 0);

        if (awaitingInput) {
          const q: string =
            (Array.isArray(meta.clarify_questions) ? meta.clarify_questions.join('\n') : '') ||
            meta.clarify_reason ||
            'The pipeline needs more information to continue.';

          setPipelineTurn(prev => prev ? {
            ...prev,
            running: false,
            awaitingInput: true,
            clarifyQuestion: q,
            stageLines: [...prev.stageLines, `[awaiting input] ${q}`],
          } : null);

          // busy stays true; user must answer
          setTimeout(() => answerInputRef.current?.focus(), 80);
        } else {
          // Truly closed / finished — treat stream-close as done
          const finalSt = res?.ticket?.status ?? 'blocked';
          setPipelineTurn(prev => prev ? {
            ...prev,
            running: false,
            finalStatus: finalSt,
            stageLines: [...prev.stageLines, 'Stream closed.'],
          } : null);
          setBusy(false);
        }
      }).catch(() => {
        // Poll failed — just close gracefully
        setPipelineTurn(prev => prev ? {
          ...prev,
          running: false,
          stageLines: [...prev.stageLines, 'Stream closed.'],
        } : null);
        setBusy(false);
      });
    };
  }

  /** Submit the user's answer to a clarification prompt and resume streaming. */
  async function submitAnswer(identifier: string, answer: string) {
    if (!answer.trim()) return;

    setPipelineTurn(prev => prev ? {
      ...prev,
      answerBusy: true,
      answersGiven: [...prev.answersGiven, answer],
      answerDraft: '',
    } : null);

    try {
      await ticketAnswer(identifier, answer);
      // Resume streaming — backend re-queued the ticket
      setPipelineTurn(prev => prev ? {
        ...prev,
        awaitingInput: false,
        clarifyQuestion: '',
        answerBusy: false,
        running: true,
        stageLines: [...prev.stageLines, `[you] ${answer}`, 'Resuming pipeline…'],
      } : null);
      openTraceStream(identifier);
    } catch (e: any) {
      toast.error(`Answer failed: ${e.message}`);
      setPipelineTurn(prev => prev ? { ...prev, answerBusy: false } : null);
    }
  }

  async function sendPipeline() {
    const q = input.trim();
    if (!q || busy) return;
    setInput('');

    // Close any prior stream
    pipelineEsRef.current?.close();
    pipelineEsRef.current = null;
    setPipelineTurn(null);

    // Ensure session
    let sessionId = activeId;
    if (sessionId === null) {
      const newId = await createSession();
      if (newId === null) return;
      sessionId = newId;
    }

    // Optimistic user message
    const optimisticUser: ChatMsg = {
      id: -(Date.now()),
      role: 'user',
      content: q,
      steps: [],
      created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, optimisticUser]);
    setBusy(true);

    const isFirstMessage = messages.length === 0;

    try {
      const proj = pipelineProject.trim() || undefined;
      const result = await chatSessionTicket(sessionId, q, proj);

      const turn: PipelineTurn = {
        ticketId: result.ticket,
        project: result.project,
        stageLines: [`Ticket ${result.ticket} created — connecting to trace…`],
        running: true,
        finalStatus: null,
        awaitingInput: false,
        clarifyQuestion: '',
        answerDraft: '',
        answerBusy: false,
        answersGiven: [],
      };
      setPipelineTurn(turn);

      openTraceStream(result.ticket);

      if (isFirstMessage) await loadSessions(true);
      else loadSessions(true);

    } catch (e: any) {
      toast.error(`Pipeline failed: ${e.message}`);
      setPipelineTurn(prev => prev ? { ...prev, running: false, stageLines: [...(prev?.stageLines ?? []), `Error: ${e.message}`] } : null);
      setBusy(false);
    }
  }

  function onKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (mode === 'pipeline') sendPipeline();
      else send();
    }
  }

  function handleSend() {
    if (mode === 'pipeline') sendPipeline();
    else send();
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
                {mode === 'pipeline' ? 'full pipeline · architect→planner→doer→learner' : 'reads & writes files · runs commands'}
              </span>
            )}
          </div>
          <div className="row" style={{ gap: 'var(--s-2)' }}>
            {/* Mode toggle */}
            <div className="chat-mode-toggle">
              <button
                className={mode === 'agent' ? 'active' : ''}
                onClick={() => setMode('agent')}
                disabled={busy}
                title="Single Doer agent — direct file/command access"
              >
                Agent
              </button>
              <button
                className={mode === 'pipeline' ? 'active' : ''}
                onClick={() => setMode('pipeline')}
                disabled={busy}
                title="Full pipeline — architect → planner → doer → learner"
              >
                Pipeline
              </button>
            </div>

            {/* Model selector (Agent mode only) */}
            {mode === 'agent' && (
              <select
                className="chat-model-select"
                value={selectedRole}
                onChange={e => setSelectedRole(e.target.value)}
                disabled={busy}
                title="Agent role / model for this conversation"
              >
                {modelOptions.length === 0 ? (
                  <option value={selectedRole}>{selectedRole}</option>
                ) : (
                  modelOptions.map(opt => (
                    <option key={opt.role} value={opt.role}>
                      {opt.role}{opt.model ? ` — ${opt.model}` : ''}
                    </option>
                  ))
                )}
              </select>
            )}

            {activeSession && (
              <>
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

              {/* Live streaming assistant turn (Agent mode) */}
              {liveTurn && (
                <div className="bubble sys">
                  <div className="bubble-avatar">AI</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <AssistantBubble
                      text={liveTurn.text}
                      steps={liveTurn.steps}
                      streaming={liveTurn.streaming}
                    />
                  </div>
                </div>
              )}

              {/* Pipeline run turn */}
              {pipelineTurn && (
                <div className="bubble sys">
                  <div className="bubble-avatar">⚡</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <PipelineBubble
                      turn={pipelineTurn}
                      answerInputRef={answerInputRef}
                      onAnswerChange={v =>
                        setPipelineTurn(prev => prev ? { ...prev, answerDraft: v } : null)
                      }
                      onAnswerSubmit={() =>
                        pipelineTurn && submitAnswer(pipelineTurn.ticketId, pipelineTurn.answerDraft)
                      }
                    />
                  </div>
                </div>
              )}
            </div>

            <div className="chat-composer" style={{ flexDirection: 'column', gap: 6 }}>
              {mode === 'pipeline' && (
                <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--fg-2)', flexShrink: 0 }}>Project:</span>
                  <input
                    type="text"
                    placeholder="optional — backend will auto-detect"
                    value={pipelineProject}
                    onChange={e => setPipelineProject(e.target.value)}
                    disabled={busy}
                    style={{
                      flex: 1,
                      fontSize: 'var(--fs-xs)',
                      padding: '3px 8px',
                      borderRadius: 'var(--r-sm)',
                      border: '1px solid var(--border-1)',
                      background: 'var(--bg-1)',
                      color: 'var(--fg-1)',
                      fontFamily: 'var(--font-mono)',
                    }}
                  />
                </div>
              )}
              <div style={{ display: 'flex', gap: 6 }}>
                <textarea
                  ref={textareaRef}
                  rows={1}
                  placeholder={mode === 'pipeline'
                    ? 'Describe the task to run through the full pipeline… (Enter to send)'
                    : 'Ask the agent to read/write files, run commands, implement a feature…  (Enter to send, Shift+Enter for newline)'}
                  value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={onKey}
                  disabled={busy}
                  style={{ flex: 1 }}
                />
                <button onClick={handleSend} disabled={busy || !input.trim()}>
                  <Icon.Agents size={14} /> {busy ? 'Running…' : mode === 'pipeline' ? 'Run Pipeline' : 'Run'}
                </button>
              </div>
            </div>
          </>
        )}

        {/* Composer shown even when no session: send will create one */}
        {activeId === null && (
          <div className="chat-composer" style={{ flexDirection: 'column', gap: 6 }}>
            {mode === 'pipeline' && (
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--fg-2)', flexShrink: 0 }}>Project:</span>
                <input
                  type="text"
                  placeholder="optional"
                  value={pipelineProject}
                  onChange={e => setPipelineProject(e.target.value)}
                  disabled={busy}
                  style={{
                    flex: 1,
                    fontSize: 'var(--fs-xs)',
                    padding: '3px 8px',
                    borderRadius: 'var(--r-sm)',
                    border: '1px solid var(--border-1)',
                    background: 'var(--bg-1)',
                    color: 'var(--fg-1)',
                    fontFamily: 'var(--font-mono)',
                  }}
                />
              </div>
            )}
            <div style={{ display: 'flex', gap: 6 }}>
              <textarea
                ref={textareaRef}
                rows={1}
                placeholder="Type a message to start a new conversation…  (Enter to send)"
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={onKey}
                disabled={busy}
                style={{ flex: 1 }}
              />
              <button onClick={handleSend} disabled={busy || !input.trim()}>
                <Icon.Agents size={14} /> {mode === 'pipeline' ? 'Run Pipeline' : 'Run'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── PipelineBubble — renders a pipeline run turn with live stage log ──────────

function PipelineBubble({
  turn,
  answerInputRef,
  onAnswerChange,
  onAnswerSubmit,
}: {
  turn: PipelineTurn;
  answerInputRef: React.RefObject<HTMLInputElement | null>;
  onAnswerChange: (v: string) => void;
  onAnswerSubmit: () => void;
}) {
  function onAnswerKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onAnswerSubmit(); }
  }

  // Determine header label
  let statusLabel: React.ReactNode = null;
  if (turn.awaitingInput) {
    statusLabel = <span style={{ color: 'var(--warn)' }}>waiting for your answer…</span>;
  } else if (turn.running) {
    statusLabel = <span style={{ color: 'var(--warn)' }}>running…</span>;
  } else if (turn.finalStatus) {
    statusLabel = <span style={{ color: 'var(--ok)' }}>✓ done ({turn.finalStatus})</span>;
  }

  return (
    <div>
      <div className="bubble-body" style={{ marginBottom: 6 }}>
        <span>
          Ticket{' '}
          <a
            href={`/ui/tickets/${turn.ticketId}`}
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: 'var(--accent)', fontWeight: 600 }}
          >
            {turn.ticketId}
          </a>
          {turn.project ? ` · ${turn.project}` : ''}
          {' '}
          {statusLabel}
        </span>
      </div>

      {/* Stage log */}
      {turn.stageLines.length > 0 && (
        <div className="pipeline-stage-log">
          {turn.stageLines.map((line, i) => (
            <div key={i} className="pipeline-stage-line">{line}</div>
          ))}
          {turn.running && !turn.awaitingInput && (
            <div style={{ padding: '4px 0' }}>
              <div className="typing"><span /><span /><span /></div>
            </div>
          )}
        </div>
      )}

      {/* Clarification answer input — shown when pipeline is awaiting input */}
      {turn.awaitingInput && (
        <div className="pipeline-clarify-box" style={{
          marginTop: 8,
          padding: '10px 12px',
          background: 'var(--bg-1)',
          border: '1px solid var(--warn)',
          borderRadius: 'var(--r-sm)',
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
        }}>
          <div style={{
            fontSize: 'var(--fs-sm)',
            color: 'var(--fg-1)',
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
          }}>
            {turn.clarifyQuestion}
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <input
              ref={answerInputRef}
              type="text"
              placeholder="Type your answer…"
              value={turn.answerDraft}
              onChange={e => onAnswerChange(e.target.value)}
              onKeyDown={onAnswerKey}
              disabled={turn.answerBusy}
              style={{
                flex: 1,
                fontSize: 'var(--fs-sm)',
                padding: '5px 10px',
                borderRadius: 'var(--r-sm)',
                border: '1px solid var(--border-1)',
                background: 'var(--bg-0)',
                color: 'var(--fg-1)',
              }}
            />
            <button
              onClick={onAnswerSubmit}
              disabled={turn.answerBusy || !turn.answerDraft.trim()}
              style={{ flexShrink: 0 }}
            >
              {turn.answerBusy ? 'Sending…' : 'Send'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── AssistantBubble — renders steps + final text ──────────────────────────────

function AssistantBubble({
  text,
  steps,
  streaming,
}: {
  text: string;
  steps: AgentStep[];
  streaming: boolean;
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
    </div>
  );
}

// ── Agent step row ─────────────────────────────────────────────────────────────

function AgentStepRow({ step }: { step: AgentStep }) {
  if (step.kind === 'thought') {
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
        <span>{step.text}</span>
      </div>
    );
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
