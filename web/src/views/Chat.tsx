import { useState } from 'react';
import { api } from '../api';

type Turn = {
  role: 'user' | 'system';
  text: string;
  // context on system turns
  queryRef?: string;
  hits?: any[];
  meta?: string;
  summary?: string;
  saved?: { worked: boolean; id?: number | string } | null;
};

// Chat = memory-grounded Q&A against AIForge Neo4j. Every user turn
// fires memory.search (vector + BM25 over T1-T4) AND graph_rag MCP
// `related_memories` for tiered context. Each system reply gets a
// "Did this work?" footer — operator confirms (or rejects) and the
// Q+A is persisted as a T3 `patterns/<topic>` memory for next time.
export default function Chat() {
  const [turns, setTurns] = useState<Turn[]>([
    {
      role: 'system',
      text: 'Hi — ask about any ticket, repo, or past decision. '
          + 'I pull from Neo4j T1-T4 memory and the graph_rag MCP. '
          + 'When a reply helps, click ✓ and I\'ll save it as a flow for next time.',
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
      // /api/chat/ask builds the context bundle (memory hits across
      // all tiers + targeted MCP tool calls) and runs the LLM against
      // it, returning a synthesized answer. UI renders the answer as
      // the primary response; hits + tools are folded into details.
      const res = await api.chatAsk(q, 16);
      const toolsSummary = (res.tools_called || []).map((t: any) => t.tool).join(', ')
        || 'none';
      const metaBits = [
        `tiers: ${(res.tiers_used || []).join(',') || '—'}`,
        `tools: ${toolsSummary}`,
      ];
      if (res.normalized) metaBits.unshift(`understood as: "${res.normalized}"`);
      setTurns(t => [
        ...t,
        {
          role: 'system',
          text: res.answer || '(no answer)',
          queryRef: res.normalized || q,
          hits: res.hits || [],
          meta: metaBits.join(' · '),
          summary: (res.answer || '').split('\n').slice(0, 2).join(' ').slice(0, 300),
          saved: null,
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

  async function retain(i: number, worked: boolean) {
    const turn = turns[i];
    if (!turn.queryRef || !turn.summary) return;
    const topic = inferTopic(turn.queryRef);
    const hitRefs = (turn.hits || [])
      .slice(0, 5)
      .map((h: any) => h.wing || h.source || String(h.id || ''))
      .filter(Boolean);
    try {
      const r = await api.chatRetain({
        query: turn.queryRef,
        answer: turn.summary,
        worked,
        topic,
        hit_refs: hitRefs,
      });
      setTurns(t => t.map((x, j) =>
        j === i ? { ...x, saved: { worked, id: r.id } } : x));
    } catch (e: any) {
      alert('retain failed: ' + e.message);
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

            {t.summary && (
              <div style={{
                marginTop: '.4rem', padding: '.4rem .6rem',
                background: '#1f2a3d', borderLeft: '3px solid #4299e1',
                fontSize: '.85rem',
              }}>
                <strong>Summary:</strong> {t.summary}
              </div>
            )}

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
              <div className="small muted" style={{ marginTop: '.3rem' }}>
                {t.meta}
              </div>
            )}

            {t.queryRef && t.saved == null && (
              <div style={{ marginTop: '.5rem' }} className="small muted">
                Did this help?{' '}
                <button onClick={() => retain(i, true)}
                        style={{ marginRight: '.3rem' }}>✓ Worked</button>
                <button onClick={() => retain(i, false)}>✘ Didn't help</button>
              </div>
            )}
            {t.saved && (
              <div className="small" style={{
                marginTop: '.5rem',
                color: t.saved.worked ? '#68d391' : '#fbd38d',
              }}>
                Saved as T3 {t.saved.worked ? 'pattern' : 'anti-pattern'} (id {t.saved.id}).
                Will surface on related queries.
              </div>
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

// Build a one-line summary from the hits. Planner-style: cite top tier
// + wing + snippet, lets future memory.search re-hit this exact flow.
function buildSummary(q: string, hits: any[], mcpHits: any): string {
  const top = (hits || []).slice(0, 3).map((h: any) => {
    const snip = (h.text || '').replace(/\s+/g, ' ').slice(0, 120);
    return `[${h.tier}:${h.wing}] ${snip}`;
  });
  const parts = [`Answered via memory hits for "${q.slice(0, 80)}".`];
  if (top.length) parts.push('Top: ' + top.join(' | '));
  if (mcpHits) {
    const mcpTop = Array.isArray(mcpHits) ? mcpHits[0] : mcpHits;
    if (mcpTop) parts.push(
      'MCP: ' + String(JSON.stringify(mcpTop)).slice(0, 160));
  }
  return parts.join(' ');
}

// Derive a short topic slug for T3 wing. Falls back to 'general'.
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

function safeJson(s: string): any {
  try { return JSON.parse(s); } catch { return s; }
}
