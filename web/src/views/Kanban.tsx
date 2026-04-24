import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';

const COLUMNS = [
  { key: 'todo',         title: 'To do' },
  { key: 'in_progress',  title: 'In progress' },
  { key: 'in_review',    title: 'In review' },
  { key: 'blocked',      title: 'Blocked' },
  { key: 'done',         title: 'Done' },
  { key: 'cancelled',    title: 'Cancelled' },
];

const PRIORITY_COLOR: Record<string, string> = {
  urgent: '#e53e3e',
  high:   '#dd6b20',
  medium: '#4299e1',
  low:    '#718096',
};

export default function Kanban() {
  const [rows, setRows] = useState<any[]>([]);
  const [q, setQ] = useState('');

  async function load() {
    setRows(await api.tickets({ limit: '200' }));
  }
  useEffect(() => { load(); }, []);

  const grouped = useMemo(() => {
    const g: Record<string, any[]> = {};
    for (const c of COLUMNS) g[c.key] = [];
    const qq = q.trim().toLowerCase();
    for (const t of rows) {
      if (qq) {
        const hay = `${t.identifier} ${t.title} ${t.project}`.toLowerCase();
        if (!hay.includes(qq)) continue;
      }
      (g[t.status] || (g[t.status] = [])).push(t);
    }
    // Within a column: priority first, then updated_at desc
    const prio: Record<string, number> = {
      urgent: 0, high: 1, medium: 2, low: 3,
    };
    for (const k of Object.keys(g)) {
      g[k].sort((a, b) =>
        (prio[a.priority] ?? 9) - (prio[b.priority] ?? 9) ||
        (b.updated_at || '').localeCompare(a.updated_at || ''));
    }
    return g;
  }, [rows, q]);

  async function setStatus(t: any, status: string) {
    await api.patch(t.identifier, { status });
    load();
  }

  return (
    <>
      <div className="row" style={{
        justifyContent: 'space-between', marginBottom: '.75rem',
      }}>
        <h1>Board</h1>
        <div className="row" style={{ gap: '.5rem' }}>
          <input placeholder="filter…" value={q}
                 onChange={e => setQ(e.target.value)} />
          <button onClick={load}>Refresh</button>
        </div>
      </div>

      <div style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${COLUMNS.length}, minmax(200px, 1fr))`,
        gap: '.5rem',
      }}>
        {COLUMNS.map(c => (
          <div key={c.key} className="card" style={{ minHeight: '60vh' }}>
            <div style={{
              display: 'flex', justifyContent: 'space-between',
              alignItems: 'center', marginBottom: '.5rem',
            }}>
              <strong>{c.title}</strong>
              <span className="muted small">{grouped[c.key]?.length || 0}</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '.4rem' }}>
              {(grouped[c.key] || []).map(t => (
                <div key={t.id} style={{
                  background: '#2d3748', padding: '.5rem',
                  borderLeft: `3px solid ${PRIORITY_COLOR[t.priority] || '#4a5568'}`,
                  borderRadius: 4,
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <Link to={`/tickets/${t.identifier}`} className="small">
                      {t.identifier}
                    </Link>
                    <span className="small muted">{t.assignee_role || '—'}</span>
                  </div>
                  <div style={{ fontSize: '.85rem', margin: '.3rem 0' }}>
                    {t.title}
                  </div>
                  <div className="row" style={{ gap: '.2rem', marginTop: '.3rem' }}>
                    {COLUMNS
                      .filter(x => x.key !== t.status)
                      .slice(0, 4)
                      .map(x => (
                        <button key={x.key}
                                onClick={() => setStatus(t, x.key)}
                                className="small"
                                style={{ fontSize: '.7rem', padding: '.15rem .4rem' }}>
                          → {x.title}
                        </button>
                      ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
