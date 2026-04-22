import { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { logStreamURL } from '../api';

const ROLES = ['supervisor', 'planner', 'doer', 'feedback', 'learner'];

export default function Logs() {
  const { role: urlRole } = useParams();
  const [role, setRole] = useState(urlRole || 'planner');
  const [lines, setLines] = useState<any[]>([]);
  const sourceRef = useRef<EventSource | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setLines([]);
    sourceRef.current?.close();
    const es = new EventSource(logStreamURL(role));
    sourceRef.current = es;
    es.onmessage = (e) => {
      try {
        const j = JSON.parse(e.data);
        setLines((prev) => [...prev.slice(-499), j]);
      } catch {
        // skip
      }
    };
    es.onerror = () => { /* auto-retries */ };
    return () => { es.close(); };
  }, [role]);

  useEffect(() => {
    boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight });
  }, [lines]);

  return (
    <>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <h1>Live logs · {role}</h1>
        <select value={role} onChange={e => setRole(e.target.value)}>
          {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
        </select>
      </div>
      <div className="card">
        <div ref={boxRef} className="event-log" style={{ maxHeight: 'calc(100vh - 220px)' }}>
          {lines.map((l, i) => (
            <div key={i} className="event-row">
              <span className="ts">{(l.ts || '').slice(11, 23)}</span>
              <span className={`chip ${levelClass(l.level)}`}>{l.level}</span>
              {l.ticket && <span className="chip">{l.ticket}</span>}
              <span className="kind" style={{ marginLeft: '.5rem' }}>{l.event}</span>
              {l.tool && <span className="muted"> {l.tool}</span>}
              {typeof l.dur_ms === 'number' && <span className="muted"> · {l.dur_ms}ms</span>}
              {typeof l.tokens_out === 'number' && <span className="muted"> · out={l.tokens_out}</span>}
              {typeof l.turn === 'number' && <span className="muted"> · turn={l.turn}</span>}
            </div>
          ))}
          {lines.length === 0 && <p className="muted">waiting for events…</p>}
        </div>
      </div>
    </>
  );
}

function levelClass(l: string) {
  if (l === 'error') return 'err';
  if (l === 'warning') return 'warn';
  return '';
}
