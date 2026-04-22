import { useState } from 'react';
import { api } from '../api';

export default function Memory() {
  const [q, setQ] = useState('');
  const [role, setRole] = useState('planner');
  const [hits, setHits] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  async function search() {
    if (q.trim().length < 2) return;
    setLoading(true);
    try {
      setHits(await api.memorySearch(q, role, 15));
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <h1>Memory search</h1>
      <div className="card">
        <div className="row">
          <input placeholder="query (e.g. stock transfer sync rules)"
                 value={q} onChange={e => setQ(e.target.value)}
                 onKeyDown={e => e.key === 'Enter' && search()}
                 style={{ flex: 1 }} />
          <select value={role} onChange={e => setRole(e.target.value)}>
            {['supervisor', 'planner', 'doer', 'feedback', 'learner'].map(r =>
              <option key={r} value={r}>{r}</option>,
            )}
          </select>
          <button onClick={search} disabled={loading}>Search</button>
        </div>
      </div>
      {hits.length > 0 && (
        <div className="card">
          <h2>{hits.length} hits</h2>
          {hits.map((h, i) => (
            <div key={i} style={{ padding: '.5rem 0', borderBottom: '1px solid var(--border)' }}>
              <div className="row small muted">
                <span className="chip">{h.tier}</span>
                <span className="chip">{h.wing}</span>
                <span>score: {Number(h.score).toFixed(3)}</span>
                {h.source && <span>· {h.source}</span>}
              </div>
              <pre>{(h.text || '').slice(0, 1500)}</pre>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
