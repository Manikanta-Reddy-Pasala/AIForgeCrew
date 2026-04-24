import { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { logStreamURL } from '../api';
import { Icon } from '../icons';

const ROLES = ['supervisor', 'planner', 'doer', 'feedback', 'learner'];

export default function Logs() {
  const { role: urlRole } = useParams();
  const [role, setRole] = useState(urlRole || 'planner');
  const [lines, setLines] = useState<any[]>([]);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState('');
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setLines([]);
    sourceRef.current?.close();
    const es = new EventSource(logStreamURL(role));
    sourceRef.current = es;
    es.onopen = () => setConnected(true);
    es.onmessage = (e) => {
      if (paused) return;
      try {
        const j = JSON.parse(e.data);
        setLines(prev => [...prev.slice(-499), j]);
      } catch { /* skip */ }
    };
    es.onerror = () => setConnected(false);
    return () => { es.close(); };
  }, [role, paused]);

  useEffect(() => {
    if (!paused) boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight });
  }, [lines, paused]);

  const shown = lines.filter(l => {
    if (!filter.trim()) return true;
    const hay = `${l.event || ''} ${l.tool || ''} ${l.ticket || ''} ${l.level || ''}`.toLowerCase();
    return hay.includes(filter.toLowerCase());
  });

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Live logs</h1>
          <div className="subtitle">
            {connected
              ? <><span style={{ color: 'var(--ok)' }}>● connected</span> — {shown.length.toLocaleString()} lines</>
              : <><span style={{ color: 'var(--warn)' }}>● reconnecting…</span></>}
          </div>
        </div>
        <div className="row">
          <select value={role} onChange={e => setRole(e.target.value)} style={{ width: 140 }}>
            {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
          <div className="input-search" style={{ minWidth: 180 }}>
            <Icon.Search size={14} />
            <input placeholder="filter events…" value={filter} onChange={e => setFilter(e.target.value)} />
          </div>
          <button className="ghost" onClick={() => setPaused(p => !p)}>
            {paused ? 'Resume' : 'Pause'}
          </button>
          <button className="ghost" onClick={() => setLines([])}>Clear</button>
        </div>
      </div>

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <div ref={boxRef} className="event-log" style={{ maxHeight: 'calc(100vh - 220px)' }}>
          {shown.length === 0 && <div className="empty">waiting for events…</div>}
          {shown.map((l, i) => (
            <div key={i} className="event-row">
              <span className="ts">{(l.ts || '').slice(11, 23)}</span>
              <span className={`chip sm ${levelClass(l.level)}`}>{l.level || 'info'}</span>
              {l.ticket && <span className="chip sm mono">{l.ticket}</span>}
              <span className="kind">{l.event}</span>
              {l.tool && <span className="muted">· {l.tool}</span>}
              {typeof l.dur_ms === 'number' && <span className="muted">· {l.dur_ms}ms</span>}
              {typeof l.tokens_out === 'number' && <span className="muted">· out={l.tokens_out}</span>}
              {typeof l.turn === 'number' && <span className="muted">· turn={l.turn}</span>}
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

function levelClass(l?: string) {
  if (l === 'error') return 'err';
  if (l === 'warning') return 'warn';
  return '';
}
