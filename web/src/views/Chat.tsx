import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { api, chatAgentURL } from '../api';
import { Icon } from '../icons';
import { MdLite } from '../mdlite';

// ── types ──────────────────────────────────────────────────────────────────────

type AgentStep =
  | { kind: 'thought'; text: string }
  | { kind: 'tool'; name: string; args: object; result: object }
  | { kind: 'error'; text: string };

type Turn = {
  id: string;
  role: 'user' | 'system';
  text: string;
  // Memory Q&A fields
  queryRef?: string;
  hits?: any[];
  tools?: any[];
  tiers?: string[];
  normalized?: string;
  summary?: string;
  saved?: { worked: boolean; id?: number | string } | null;
  // Agent mode fields
  agentMode?: boolean;
  steps?: AgentStep[];
  streaming?: boolean;
  createdAt: number;
};

type ChatMode = 'memory' | 'agent';

function uid() { return Math.random().toString(36).slice(2, 10); }

// Chat = memory-grounded Q&A against AIForge Neo4j + graph_rag MCP.
// Agent mode adds a full-filesystem coding agent via POST /api/chat/agent (SSE).
export default function Chat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [drawerTurn, setDrawerTurn] = useState<Turn | null>(null);
  const [mode, setMode] = useState<ChatMode>(() => {
    try {
      const saved = localStorage.getItem('aiforge.chat.mode');
      return (saved === 'agent' || saved === 'memory') ? saved : 'memory';
    } catch { return 'memory'; }
  });
  const logRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // History rehydration from localStorage. Chat UI state is ephemeral
  // by policy — drop turns older than 7 days on load. Actual memory +
  // confirmed flows live in Neo4j (T1-T3) and are unaffected.
  useEffect(() => {
    const CHAT_TTL_MS = 7 * 24 * 60 * 60 * 1000;
    try {
      const raw = localStorage.getItem('aiforge.chat.history');
      if (!raw) return;
      const saved = JSON.parse(raw) as Turn[];
      if (!Array.isArray(saved)) return;
      const cutoff = Date.now() - CHAT_TTL_MS;
      const fresh = saved.filter(t => (t.createdAt || 0) >= cutoff);
      if (fresh.length > 0) setTurns(fresh);
      if (fresh.length !== saved.length) {
        localStorage.setItem('aiforge.chat.history', JSON.stringify(fresh));
      }
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    try {
      const toSave = turns.slice(-60).map(t => ({ ...t, streaming: false }));
      localStorage.setItem('aiforge.chat.history', JSON.stringify(toSave));
      localStorage.setItem('aiforge.chat.saved_at', String(Date.now()));
    } catch { /* ignore */ }
  }, [turns]);

  useEffect(() => {
    try { localStorage.setItem('aiforge.chat.mode', mode); } catch { /* ignore */ }
  }, [mode]);

  // auto-scroll on new turns
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' });
  }, [turns, busy]);

  function resetChat() {
    setTurns([]);
    setDrawerTurn(null);
    try { localStorage.removeItem('aiforge.chat.history'); } catch { /* ignore */ }
  }

  // ── Memory Q&A mode ────────────────────────────────────────────────────────

  async function sendMemory(q: string) {
    const userTurn: Turn = { id: uid(), role: 'user', text: q, createdAt: Date.now() };
    setTurns(t => [...t, userTurn]);
    setBusy(true);
    try {
      const res = await api.chatAsk(q, 16);
      const sysTurn: Turn = {
        id: uid(),
        role: 'system',
        text: res.answer || '(no answer)',
        queryRef: res.normalized || q,
        normalized: res.normalized,
        hits: res.hits || [],
        tools: res.tools_called || [],
        tiers: res.tiers_used || [],
        summary: (res.answer || '').split('\n').slice(0, 2).join(' ').slice(0, 300),
        saved: null,
        createdAt: Date.now(),
      };
      setTurns(t => [...t, sysTurn]);
      setDrawerTurn(sysTurn);
    } catch (e: any) {
      setTurns(t => [...t, {
        id: uid(),
        role: 'system',
        text: `error: ${e.message}`,
        createdAt: Date.now(),
      }]);
      toast.error(`Chat failed: ${e.message}`);
    } finally {
      setBusy(false);
      textareaRef.current?.focus();
    }
  }

  // ── Agent mode (SSE over POST) ─────────────────────────────────────────────

  async function sendAgent(q: string, allTurns: Turn[]) {
    // Build message history: include all prior turns except in-progress ones
    const messages = allTurns
      .filter(t => !t.streaming)
      .map(t => ({
        role: (t.role === 'user' ? 'user' : 'assistant') as 'user' | 'assistant',
        content: t.text,
      }));
    // Append the new user message
    messages.push({ role: 'user', content: q });

    const agentTurnId = uid();
    const agentTurn: Turn = {
      id: agentTurnId,
      role: 'system',
      text: '',
      agentMode: true,
      steps: [],
      streaming: true,
      createdAt: Date.now(),
    };
    setBusy(true);

    setTurns(t => [...t, agentTurn]);

    try {
      const res = await fetch(chatAgentURL(), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages }),
      });

      if (!res.ok) {
        let detail = '';
        try { const b = await res.json(); detail = b?.detail || b?.error || ''; } catch { /* ignore */ }
        try { if (!detail) detail = await res.text(); } catch { /* ignore */ }
        throw new Error(`${res.status} ${res.statusText}${detail ? ` — ${detail}` : ''}`);
      }

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      function applyEvent(raw: string) {
        // Strip leading "data: " prefix
        const line = raw.startsWith('data: ') ? raw.slice(6) : raw;
        if (!line.trim()) return;
        let evt: any;
        try { evt = JSON.parse(line); } catch { return; }

        setTurns(ts => ts.map(t => {
          if (t.id !== agentTurnId) return t;
          if (evt.type === 'thought') {
            return { ...t, steps: [...(t.steps || []), { kind: 'thought', text: evt.text }] };
          }
          if (evt.type === 'tool') {
            return { ...t, steps: [...(t.steps || []), { kind: 'tool', name: evt.name, args: evt.args || {}, result: evt.result || {} }] };
          }
          if (evt.type === 'message') {
            return { ...t, text: evt.text, streaming: false };
          }
          if (evt.type === 'error') {
            return { ...t, text: evt.text, steps: [...(t.steps || []), { kind: 'error', text: evt.text }], streaming: false };
          }
          if (evt.type === 'done') {
            return { ...t, streaming: false };
          }
          return t;
        }));
      }

      outer: while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // Split on double newline (SSE event boundary)
        const parts = buffer.split('\n\n');
        // Last part may be incomplete — keep it in the buffer
        buffer = parts.pop() ?? '';
        for (const part of parts) {
          if (part.trim()) {
            // Each part may contain multiple "data: ..." lines — handle the
            // common single-line case and the rare multi-line case.
            for (const line of part.split('\n')) {
              if (line.startsWith('data: ')) {
                applyEvent(line);
              }
            }
          }
          // Check if we already got done/message from this batch
        }
      }

      // Flush any remaining buffer content
      if (buffer.trim()) {
        for (const line of buffer.split('\n')) {
          if (line.startsWith('data: ')) applyEvent(line);
        }
      }

      // Ensure streaming flag is cleared regardless of whether 'done' arrived
      setTurns(ts => ts.map(t => t.id === agentTurnId ? { ...t, streaming: false } : t));
    } catch (e: any) {
      setTurns(ts => ts.map(t =>
        t.id === agentTurnId
          ? { ...t, text: `Agent error: ${e.message}`, streaming: false }
          : t
      ));
      toast.error(`Agent failed: ${e.message}`);
    } finally {
      setBusy(false);
      textareaRef.current?.focus();
    }
  }

  // ── Shared send handler ────────────────────────────────────────────────────

  async function send() {
    const q = input.trim();
    if (!q || busy) return;
    setInput('');
    if (mode === 'agent') {
      // Snapshot turns before appending the new user message so we can build history
      const snapshot = turns;
      const userTurn: Turn = { id: uid(), role: 'user', text: q, createdAt: Date.now() };
      setTurns(t => [...t, userTurn]);
      await sendAgent(q, snapshot);
    } else {
      await sendMemory(q);
    }
  }

  async function retain(turn: Turn, worked: boolean) {
    if (!turn.queryRef || !turn.summary) return;
    const topic = inferTopic(turn.queryRef);
    const hitRefs = (turn.hits || [])
      .slice(0, 5)
      .map(h => h.wing || h.source || String(h.id || ''))
      .filter(Boolean);
    try {
      const r = await api.chatRetain({
        query: turn.queryRef,
        answer: turn.summary,
        worked,
        topic,
        hit_refs: hitRefs,
      });
      setTurns(ts => ts.map(x =>
        x.id === turn.id ? { ...x, saved: { worked, id: r.id } } : x));
      toast.success(worked
        ? `Saved as pattern (${topic})`
        : `Marked as anti-pattern (${topic})`);
    } catch (e: any) {
      toast.error(`Retain failed: ${e.message}`);
    }
  }

  function onKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  const drawerOpen = !!drawerTurn && mode === 'memory';

  return (
    <div className={`chat-shell ${drawerOpen ? '' : 'drawer-closed'}`}>
      <section className="chat-main">
        <div className="chat-topbar">
          <div className="row">
            {/* Mode toggle */}
            <div className="segmented">
              <button
                className={mode === 'memory' ? 'on' : ''}
                onClick={() => setMode('memory')}
                title="Memory-grounded Q&A"
              >
                <Icon.Sparkles size={11} /> Ask memory
              </button>
              <button
                className={mode === 'agent' ? 'on' : ''}
                onClick={() => setMode('agent')}
                title="Full-filesystem coding agent"
              >
                <Icon.Agents size={11} /> Agent (full FS)
              </button>
            </div>
            {mode === 'agent' && (
              <span className="xs muted" style={{ fontFamily: 'var(--font-mono)' }}>
                reads &amp; writes files · runs commands
              </span>
            )}
          </div>
          <div className="row">
            <button className="ghost sm" onClick={resetChat}>Clear</button>
            {mode === 'memory' && (
              <button
                className="ghost sm"
                onClick={() => setDrawerTurn(drawerOpen ? null : (turns.slice().reverse().find(t => t.role === 'system' && t.hits) || null))}
              >
                {drawerOpen ? 'Hide context' : 'Show context'}
              </button>
            )}
          </div>
        </div>

        <div className="chat-log" ref={logRef}>
          {turns.map(t => (
            <div key={t.id} className={`bubble ${t.role === 'user' ? 'user' : 'sys'}`}>
              <div className="bubble-avatar">{t.role === 'user' ? 'You' : 'AI'}</div>
              <div style={{ flex: 1, minWidth: 0 }}>
                {/* Agent turn: show steps then final answer */}
                {t.agentMode ? (
                  <div>
                    {/* Streamed steps */}
                    {(t.steps || []).length > 0 && (
                      <div style={{ marginBottom: t.text ? 8 : 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
                        {(t.steps || []).map((s, i) => (
                          <AgentStepRow key={i} step={s} />
                        ))}
                      </div>
                    )}
                    {/* Final message */}
                    {t.text && (
                      <div className="bubble-body">
                        <MdLite text={t.text} />
                      </div>
                    )}
                    {/* Still streaming with no answer yet */}
                    {t.streaming && !t.text && (t.steps || []).length === 0 && (
                      <div className="bubble-body" style={{ padding: 0, background: 'transparent', border: 0 }}>
                        <div className="typing"><span /><span /><span /></div>
                      </div>
                    )}
                  </div>
                ) : (
                  <div>
                    <div className="bubble-body">
                      {t.role === 'system'
                        ? <MdLite text={t.text} />
                        : <span style={{ whiteSpace: 'pre-wrap' }}>{t.text}</span>}
                    </div>
                    {t.role === 'system' && t.queryRef && (
                      <div className="bubble-footer">
                        {t.normalized && t.normalized !== t.text && (
                          <span className="xs muted">understood as "{t.normalized}"</span>
                        )}
                        {t.tiers && t.tiers.length > 0 && (
                          <span className="chip sm">tiers: {t.tiers.join(',')}</span>
                        )}
                        {t.tools && t.tools.length > 0 && (
                          <span className="chip sm">{t.tools.length} tool call{t.tools.length === 1 ? '' : 's'}</span>
                        )}
                        {t.hits && t.hits.length > 0 && (
                          <button className="ghost sm" onClick={() => setDrawerTurn(t)}>
                            {t.hits.length} hits
                          </button>
                        )}
                        <span className="spacer" />
                        {t.saved == null && (
                          <span className="retain-row">
                            <button className="ghost sm" onClick={() => retain(t, true)}>
                              <Icon.Check size={12} /> Worked
                            </button>
                            <button className="ghost sm" onClick={() => retain(t, false)}>
                              <Icon.X size={12} /> Didn't help
                            </button>
                          </span>
                        )}
                        {t.saved && (
                          <span className={`chip sm ${t.saved.worked ? 'ok' : 'warn'}`}>
                            saved as T3 {t.saved.worked ? 'pattern' : 'anti-pattern'}
                            {t.saved.id != null ? ` #${t.saved.id}` : ''}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          ))}
          {busy && mode === 'memory' && (
            <div className="bubble sys">
              <div className="bubble-avatar">AI</div>
              <div className="bubble-body" style={{ padding: 0, background: 'transparent', border: 0 }}>
                <div className="typing"><span /><span /><span /></div>
              </div>
            </div>
          )}
        </div>

        <div className="chat-composer">
          <textarea
            ref={textareaRef}
            rows={1}
            placeholder={
              mode === 'agent'
                ? 'Ask the agent to read/write files, run commands, implement a feature…  (Enter to send)'
                : 'Ask about tickets, repos, code, past decisions…  (Enter to send, Shift+Enter for newline)'
            }
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={onKey}
            disabled={busy}
          />
          <button onClick={send} disabled={busy || !input.trim()}>
            {mode === 'agent'
              ? <><Icon.Agents size={14} /> {busy ? 'Running…' : 'Run'}</>
              : <><Icon.Send size={14} /> {busy ? 'Thinking' : 'Send'}</>
            }
          </button>
        </div>
      </section>

      {drawerOpen && drawerTurn && (
        <aside className="chat-drawer">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <h3>Context</h3>
            <button className="icon" onClick={() => setDrawerTurn(null)} title="Close"><Icon.X size={14} /></button>
          </div>
          <div className="drawer-section">
            <h4>Query</h4>
            <div className="small">{drawerTurn.queryRef || '—'}</div>
            {drawerTurn.tiers && drawerTurn.tiers.length > 0 && (
              <div style={{ marginTop: 6 }} className="row tight">
                {drawerTurn.tiers.map(tr => <span key={tr} className="chip sm">{tr}</span>)}
              </div>
            )}
          </div>
          {drawerTurn.tools && drawerTurn.tools.length > 0 && (
            <div className="drawer-section">
              <h4>Tool calls ({drawerTurn.tools.length})</h4>
              {drawerTurn.tools.map((c: any, i: number) => (
                <div key={i} className="hit">
                  <div className="hit-head">
                    <span className="chip sm mono">{c.tool}</span>
                    {typeof c.dur_ms === 'number' && <span>· {c.dur_ms}ms</span>}
                  </div>
                  {c.args && <pre className="xs" style={{ padding: 6, margin: 0 }}>{JSON.stringify(c.args, null, 2)}</pre>}
                </div>
              ))}
            </div>
          )}
          <div className="drawer-section">
            <h4>Memory hits ({drawerTurn.hits?.length || 0})</h4>
            {(drawerTurn.hits || []).slice(0, 20).map((h: any, i: number) => (
              <div key={i} className="hit">
                <div className="hit-head">
                  <span className="chip sm">{h.tier}</span>
                  <span className="chip sm mono">{h.wing}</span>
                  {typeof h.score === 'number' && <span>score {h.score.toFixed(3)}</span>}
                </div>
                <div>{(h.text || '').slice(0, 280)}</div>
              </div>
            ))}
            {(!drawerTurn.hits || drawerTurn.hits.length === 0) && (
              <div className="muted small">No hits returned.</div>
            )}
          </div>
        </aside>
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

function inferTopic(q: string): string {
  const keywords: Record<string, string> = {
    'pagination|batchSize|limit': 'pagination',
    'memory|neo4j|mcp':           'memory',
    'sync|nats|pushToRemote':     'sync',
    'readme|doc(s|ument)':        'docs',
    'compile|mvn|build':          'build',
    'deploy|k8s|kubernetes':      'deploy',
    'sales|invoice|balance':      'finance-flow',
  };
  const ql = q.toLowerCase();
  for (const pat of Object.keys(keywords)) {
    if (new RegExp(pat).test(ql)) return keywords[pat];
  }
  return 'general';
}
