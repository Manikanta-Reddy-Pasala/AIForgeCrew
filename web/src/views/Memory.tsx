import { useState } from 'react';
import { api } from '../api';
import { Icon } from '../icons';

const ROLES = ['supervisor', 'planner', 'doer', 'feedback', 'learner'];

export default function Memory() {
  const [q, setQ] = useState('');
  const [role, setRole] = useState('planner');
  const [hits, setHits] = useState<any[] | null>(null);
  const [loading, setLoading] = useState(false);

  async function search() {
    if (q.trim().length < 2) return;
    setLoading(true);
    try {
      const r = await api.memorySearch(q, role, 15);
      setHits(r);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Memory search</h1>
          <div className="subtitle">Hybrid vector + BM25 across T1–T4 across all wings, scoped to a role profile.</div>
        </div>
      </div>

      <div className="card">
        <div className="row">
          <div className="input-search" style={{ flex: 1, minWidth: 300 }}>
            <Icon.Search size={14} />
            <input
              placeholder="query (e.g. stock transfer sync rules)"
              value={q}
              onChange={e => setQ(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && search()}
              autoFocus
            />
          </div>
          <label className="field" style={{ flexDirection: 'row', alignItems: 'center' }}>
            Role
            <select value={role} onChange={e => setRole(e.target.value)} style={{ minWidth: 130 }}>
              {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <button onClick={search} disabled={loading || q.trim().length < 2}>
            {loading ? 'Searching…' : <><Icon.Search size={14} /> Search</>}
          </button>
        </div>
      </div>

      {hits !== null && (
        hits.length === 0 ? (
          <div className="card">
            <div className="empty">
              <div className="empty-icon">∅</div>
              <div style={{ color: 'var(--fg-0)', fontWeight: 500 }}>No hits</div>
              <div>Try a broader query or a different role.</div>
            </div>
          </div>
        ) : (
          <div className="card">
            <div className="card-header">
              <h2>{hits.length} hits</h2>
              <span className="muted small">role: <code>{role}</code></span>
            </div>
            <div className="stack">
              {hits.map((h: any, i: number) => (
                <div key={i} style={{ paddingBottom: 12, borderBottom: '1px solid var(--border-0)' }}>
                  <div className="row tight" style={{ marginBottom: 6 }}>
                    <span className="chip sm">{h.tier}</span>
                    <span className="chip sm mono">{h.wing}</span>
                    <span className="muted xs mono">score {Number(h.score).toFixed(3)}</span>
                    {h.source && <span className="muted xs">· {h.source}</span>}
                  </div>
                  <pre style={{ margin: 0, fontSize: 12 }}>{(h.text || '').slice(0, 1500)}</pre>
                </div>
              ))}
            </div>
          </div>
        )
      )}
    </>
  );
}
