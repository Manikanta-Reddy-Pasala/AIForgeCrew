import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { api } from '../api';
import { Icon } from '../icons';
import { MdLite } from '../mdlite';

type Turn = {
  id: string;
  role: 'user' | 'system';
  text: string;
  queryRef?: string;
  hits?: any[];
  tools?: any[];
  tiers?: string[];
  normalized?: string;
  summary?: string;
  saved?: { worked: boolean; id?: number | string } | null;
  createdAt: number;
};

function uid() { return Math.random().toString(36).slice(2, 10); }

// Chat = memory-grounded Q&A against AIForge Neo4j + graph_rag MCP.
// Each system reply has a "did this help?" footer that persists the Q+A
// as a T3 `patterns/<topic>` memory via /chat/retain.
export default function Chat() {
  const [turns, setTurns] = useState<Turn[]>([
    {
      id: uid(),
      role: 'system',
      text: 'Hi — ask about any ticket, repo, or past decision. I pull from Neo4j T1–T4 memory and the `graph_rag` MCP. When a reply helps, tap **Worked** and I will save it as a flow for next time.',
      createdAt: Date.now(),
    },
  ]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [drawerTurn, setDrawerTurn] = useState<Turn | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // history rehydration (localStorage)
  useEffect(() => {
    try {
      const raw = localStorage.getItem('aiforge.chat.history');
      if (raw) {
        const saved = JSON.parse(raw) as Turn[];
        if (Array.isArray(saved) && saved.length > 0) setTurns(saved);
      }
    } catch { /* ignore */ }
  }, []);
  useEffect(() => {
    try {
      // keep only last 60 turns to avoid unbounded growth
      const toSave = turns.slice(-60);
      localStorage.setItem('aiforge.chat.history', JSON.stringify(toSave));
    } catch { /* ignore */ }
  }, [turns]);

  // auto-scroll on new turns
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' });
  }, [turns, busy]);

  function resetChat() {
    setTurns([{
      id: uid(),
      role: 'system',
      text: 'Cleared. Ask away.',
      createdAt: Date.now(),
    }]);
    setDrawerTurn(null);
  }

  async function send() {
    const q = input.trim();
    if (!q || busy) return;
    setInput('');
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

  const drawerOpen = !!drawerTurn;

  return (
    <div className={`chat-shell ${drawerOpen ? '' : 'drawer-closed'}`}>
      <section className="chat-main">
        <div className="chat-topbar">
          <div className="row">
            <h2><Icon.Sparkles size={14} style={{ display: 'inline', verticalAlign: '-2px', marginRight: 6 }} /> Memory-grounded chat</h2>
          </div>
          <div className="row">
            <button className="ghost sm" onClick={resetChat}>Clear</button>
            <button
              className="ghost sm"
              onClick={() => setDrawerTurn(drawerOpen ? null : (turns.slice().reverse().find(t => t.role === 'system' && t.hits) || null))}
            >
              {drawerOpen ? 'Hide context' : 'Show context'}
            </button>
          </div>
        </div>

        <div className="chat-log" ref={logRef}>
          {turns.map(t => (
            <div key={t.id} className={`bubble ${t.role}`}>
              <div className="bubble-avatar">{t.role === 'user' ? 'You' : 'AI'}</div>
              <div style={{ flex: 1, minWidth: 0 }}>
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
            </div>
          ))}
          {busy && (
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
            placeholder="Ask about tickets, repos, code, past decisions…  (Enter to send, Shift+Enter for newline)"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={onKey}
            disabled={busy}
          />
          <button onClick={send} disabled={busy || !input.trim()}>
            <Icon.Send size={14} /> {busy ? 'Thinking' : 'Send'}
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
