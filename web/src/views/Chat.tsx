import { useState } from 'react';
import { api } from '../api';

type Turn = {
  role: 'user' | 'system';
  text: string;
  hits?: any[];
  meta?: string;
};

// Chat = memory-grounded Q&A against the AIForge Neo4j. Every user turn
// fires both `memory.search` (vector + BM25 over T1-T4) and the
// graph_rag MCP `related_memories` tool so we get tiered hits. Results
// render as a chat message with inline snippets the operator can click
// to open the source ticket / repo.
export default function Chat() {
  const [turns, setTurns] = useState<Turn[]>([
    {
      role: 'system',
      text: 'Hi — ask a question about any ticket, repo, or past decision. '
          + 'I pull from Neo4j T1-T4 memory and the graph_rag MCP.',
    },
  ]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);

  async function send() {
    const q = input.trim();
    if (!q) return;
    setInput('');
    setTurns(t => [...t, { role: 'user', text: q }]);
    setBusy(true);
    try {
      const [hits, mcp] = await Promise.all([
        api.memorySearch(q, 'planner', 12),
        api.mcpTool('related_memories', { query: q, top_k: 6 }).catch(() => null),
      ]);
      const mcpHits = mcp?.result?.content?.[0]?.text
        ? safeJson(mcp.result.content[0].text)
        : null;
      setTurns(t => [
        ...t,
        {
          role: 'system',
          text: `${hits.length} memory hits + ${mcpHits ? 'MCP related' : 'no-MCP'}.`,
          hits,
          meta: mcpHits ? JSON.stringify(mcpHits).slice(0, 1200) : '',
        },
      ]);
    } catch (e: any) {
      setTurns(t => [
        ...t,
        { role: 'system', text: `error: ${e.message}` },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Chat</h1>
      <div className="card" style={{ maxHeight: '60vh', overflow: 'auto' }}>
        {turns.map((t, i) => (
          <div key={i} style={{ marginBottom: '.75rem' }}>
            <span className="chip" style={{
              background: t.role === 'user' ? '#2a4365' : '#2d3748',
              marginRight: '.5rem',
            }}>{t.role}</span>
            <span>{t.text}</span>
            {t.hits && t.hits.length > 0 && (
              <ul style={{ marginTop: '.5rem', paddingLeft: '1rem' }}>
                {t.hits.slice(0, 8).map((h: any, j: number) => (
                  <li key={j} className="small muted">
                    <strong>[{h.tier}]</strong> {h.wing} —{' '}
                    {h.text?.slice(0, 180)}
                  </li>
                ))}
              </ul>
            )}
            {t.meta && (
              <details>
                <summary className="muted small">MCP related_memories</summary>
                <pre className="small" style={{
                  background: '#1a1a1a', padding: '.5rem',
                  overflow: 'auto', maxHeight: '200px',
                }}>{t.meta}</pre>
              </details>
            )}
          </div>
        ))}
      </div>

      <div className="card">
        <div className="row" style={{ gap: '.5rem' }}>
          <input
            placeholder="Ask about tickets, repos, code, decisions…"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !busy && send()}
            style={{ flex: 1 }}
            disabled={busy}
          />
          <button onClick={send} disabled={busy || !input.trim()}>
            {busy ? 'Thinking…' : 'Send'}
          </button>
        </div>
      </div>
    </>
  );
}

function safeJson(s: string): any {
  try { return JSON.parse(s); } catch { return s; }
}
