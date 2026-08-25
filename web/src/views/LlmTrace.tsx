// LlmTrace — per-ticket LLM round-trip viewer.
//
// Loads /api/llm-trace/{id} (last N events) and optionally subscribes to
// /api/llm-trace/{id}/stream for live appending. Each round-trip card
// shows agent role, dur_ms, expandable system/user/assistant message
// chain — the same data `aiforge ticket llm-trace --full` prints.
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

type Msg = { role?: string; content?: string };
type LlmCall = {
  agent_role?: string;
  ticket?: string;
  model?: string;
  provider?: string;
  dur_ms?: number;
  messages?: Msg[];
  response?: string;
  error?: string;
  ts?: string;
};

export default function LlmTrace() {
  const { id = '' } = useParams();
  const [events, setEvents] = useState<LlmCall[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [follow, setFollow] = useState(false);
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});

  // Initial backlog fetch.
  useEffect(() => {
    if (!id) return;
    setLoading(true);
    fetch(`/api/llm-trace/${id}?limit=50`)
      .then(r => r.json())
      .then(d => {
        if (d.error) setErr(d.error);
        setEvents(d.events || []);
      })
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [id]);

  // Optional live tail.
  useEffect(() => {
    if (!id || !follow) return;
    const es = new EventSource(`/api/llm-trace/${id}/stream`);
    es.onmessage = e => {
      try {
        const d = JSON.parse(e.data);
        const line: string = d.line || '';
        if (!line) return;
        const j = JSON.parse(line);
        // graph-runner.err wraps the call payload in extra.aiforge.
        const call = (j?.extra?.aiforge || j) as LlmCall;
        if (!call?.agent_role) return;
        setEvents(s => [...s, call]);
      } catch {/* ignore */}
    };
    return () => es.close();
  }, [id, follow]);

  return (
    <>
      <div className="page-header">
        <div>
          <h1>LLM trace · {id}</h1>
          <div className="subtitle">
            Full chat history per agent round-trip. {events.length} events captured.
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <label className="row" style={{ gap: 6 }}>
            <input type="checkbox" checked={follow}
                   onChange={e => setFollow(e.target.checked)} />
            <span className="small">live tail</span>
          </label>
          <Link to={`/trace/${id}`} className="ghost sm" style={{
            padding: '4px 10px', border: '1px solid var(--border-1)', borderRadius: 4,
          }}>full trace</Link>
          <Link to={`/tickets/${id}`} className="ghost sm" style={{
            padding: '4px 10px', border: '1px solid var(--border-1)', borderRadius: 4,
          }}>ticket</Link>
        </div>
      </div>

      {loading && <div className="card muted">Loading…</div>}
      {err && <div className="card" style={{ color: 'var(--err)' }}>error: {err}</div>}

      {!loading && events.length === 0 && (
        <div className="card muted small">
          No <code>llm.call</code> events yet for {id}. (They appear as soon
          as any agent makes an LLM call. If this ticket finished before the
          GA llm-trace wiring shipped — 2026-04-26 — the history is empty.)
        </div>
      )}

      <div style={{ maxHeight: '75vh', overflow: 'auto' }}>
        {events.map((c, i) => {
          const open = !!expanded[i];
          return (
            <div key={c.ts ?? `${c.agent_role ?? ''}-${c.dur_ms ?? ''}`} className="card" style={{ marginBottom: 8 }}>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <div className="row" style={{ gap: 8 }}>
                  <strong>#{i + 1}</strong>
                  <span className="chip sm" style={{ fontWeight: 600 }}>
                    {c.agent_role || '?'}
                  </span>
                  {c.model && (
                    <span className="muted small mono">
                      {String(c.model).split('/').pop()}
                    </span>
                  )}
                  {c.error && (
                    <span className="chip sm err">err: {c.error.slice(0, 80)}</span>
                  )}
                </div>
                <div className="small muted">
                  {c.dur_ms != null ? `${c.dur_ms}ms` : ''}
                  {c.messages && ` · msgs=${c.messages.length}`}
                  {c.response != null && ` · resp=${c.response.length}c`}
                  <button type="button" className="ghost sm" style={{ marginLeft: 8 }}
                          onClick={() => setExpanded(s => ({...s, [i]: !open}))}>
                    {open ? 'collapse' : 'expand'}
                  </button>
                </div>
              </div>
              {open && (
                <div style={{ marginTop: 8 }}>
                  {/* key=index: immutable per-call transcript rendered once;
                      roles/content duplicate across turns and it never reorders,
                      so a content key would collide. (S6479 exception) */}
                  {(c.messages || []).map((m, j) => (
                    <details key={j} style={{ marginBottom: 6 }}>
                      <summary className="small">
                        <strong>→ {m.role || '?'}</strong>
                        <span className="muted">
                          {' '}({(m.content || '').length} chars)
                        </span>
                      </summary>
                      <pre className="small" style={{
                        background: 'var(--bg-code)', padding: 8,
                        marginTop: 4, overflow: 'auto', maxHeight: 360,
                        border: '1px solid var(--border-0)', borderRadius: 4,
                      }}>{m.content || '(empty)'}</pre>
                    </details>
                  ))}
                  {c.response != null && (
                    <details open style={{ marginBottom: 6 }}>
                      <summary className="small">
                        <strong>← assistant</strong>
                        <span className="muted">
                          {' '}({(c.response || '').length} chars)
                        </span>
                      </summary>
                      <pre className="small" style={{
                        background: 'var(--bg-code)', padding: 8,
                        marginTop: 4, overflow: 'auto', maxHeight: 360,
                        border: '1px solid var(--border-0)', borderRadius: 4,
                      }}>{c.response}</pre>
                    </details>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </>
  );
}
