import { useEffect, useRef, useState } from 'react';
import { useParams, Link } from 'react-router-dom';

// Trace = live SSE stream of graph-runner log lines filtered by ticket.
// Each 'Step N' divider opens a new card; subsequent lines (action code,
// tool call, observation, duration/tokens footer) attach to that card.
// Operator gets per-step visibility + can cancel the ticket if stuck.

type StepCard = {
  n: number;
  started: number;
  lines: string[];
  durationMs?: number;
  tokens?: string;
};

export default function Trace() {
  const { id } = useParams<{ id: string }>();
  const [steps, setSteps] = useState<StepCard[]>([]);
  const [connected, setConnected] = useState(false);
  const [auto, setAuto] = useState(true);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!id) return;
    const es = new EventSource(`/api/trace/${id}/stream`);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = ev => {
      try {
        const d = JSON.parse(ev.data);
        const line: string = d.line || '';
        if (!line) return;
        // Step divider — open new card
        const stepMatch = line.match(/Step (\d+)\s/);
        if (stepMatch) {
          const n = parseInt(stepMatch[1], 10);
          setSteps(s => [...s, { n, started: Date.now(), lines: [] }]);
          return;
        }
        // Duration + tokens — attach to last card
        const durMatch = line.match(/Step \d+: Duration ([\d.]+) seconds\| Input tokens: ([\d,]+) \| Output tokens: ([\d,]+)/);
        if (durMatch) {
          setSteps(s => {
            if (s.length === 0) return s;
            const last = { ...s[s.length - 1] };
            last.durationMs = parseFloat(durMatch[1]) * 1000;
            last.tokens = `in=${durMatch[2]} out=${durMatch[3]}`;
            return [...s.slice(0, -1), last];
          });
          return;
        }
        // Regular line — attach to last card (or stray)
        setSteps(s => {
          if (s.length === 0) return [{ n: 0, started: Date.now(), lines: [line] }];
          const last = { ...s[s.length - 1], lines: [...s[s.length - 1].lines, line] };
          return [...s.slice(0, -1), last];
        });
      } catch { /* ignore */ }
    };
    return () => es.close();
  }, [id]);

  useEffect(() => {
    if (auto && logRef.current) {
      logRef.current.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' });
    }
  }, [steps, auto]);

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Trace · {id}</h1>
          <div className="subtitle">
            Live agent steps from graph-runner. Each card = one ReAct step.
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <span className={`health-dot ${connected ? '' : 'down'}`}>
            <span className="dot" /> {connected ? 'streaming' : 'disconnected'}
          </span>
          <label className="row" style={{ gap: 6 }}>
            <input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} />
            <span className="small">auto-scroll</span>
          </label>
          <Link to={`/tickets/${id}`} className="ghost sm" style={{
            padding: '4px 10px', border: '1px solid var(--border-1)', borderRadius: 4,
          }}>ticket</Link>
        </div>
      </div>

      <div ref={logRef} style={{ maxHeight: '75vh', overflow: 'auto' }}>
        {steps.length === 0 && (
          <div className="card muted small">Waiting for graph-runner…</div>
        )}
        {steps.map((s, i) => (
          <div key={i} className="card" style={{ marginBottom: 8 }}>
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <strong>Step {s.n || '—'}</strong>
              <div className="small muted">
                {s.durationMs != null ? `${(s.durationMs/1000).toFixed(1)}s` : 'running…'}
                {s.tokens && ` · ${s.tokens}`}
              </div>
            </div>
            {(s.lines?.length ?? 0) > 0 && (
              <pre className="small" style={{
                background: 'var(--bg-code)',
                padding: 8,
                marginTop: 8,
                overflow: 'auto',
                maxHeight: 260,
                border: '1px solid var(--border-0)',
                borderRadius: 4,
              }}>{s.lines.join('\n')}</pre>
            )}
          </div>
        ))}
      </div>
    </>
  );
}
