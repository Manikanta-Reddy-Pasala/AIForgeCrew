import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';

export default function Dashboard() {
  const [health, setHealth] = useState<any>(null);
  const [agents, setAgents] = useState<any[]>([]);
  const [tickets, setTickets] = useState<any[]>([]);
  const [mem, setMem] = useState<any>({ wings: [] });

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth({ ok: false }));
    api.agents().then(setAgents).catch(() => setAgents([]));
    api.tickets({ limit: '15' }).then(setTickets).catch(() => setTickets([]));
    api.memoryStats().then(setMem).catch(() => setMem({ wings: [] }));
  }, []);

  const totalMem = mem.wings.reduce((a: number, w: any) => a + Number(w.n), 0);
  const embedded = mem.wings.reduce((a: number, w: any) => a + Number(w.embedded), 0);

  return (
    <>
      <h1>AIForge v5</h1>
      <div className="grid grid-4">
        <div className="card">
          <h2>Postgres</h2>
          <span className={`chip ${health?.postgres ? 'ok' : 'err'}`}>
            {health?.postgres ? 'online' : 'down'}
          </span>
        </div>
        <div className="card">
          <h2>LM Studio</h2>
          <span className={`chip ${health?.lm_studio ? 'ok' : 'err'}`}>
            {health?.lm_studio ? 'online' : 'down'}
          </span>
        </div>
        <div className="card">
          <h2>Agents</h2>
          <div className="small muted">{agents.length} roles</div>
        </div>
        <div className="card">
          <h2>Memory</h2>
          <div className="small muted">{totalMem.toLocaleString()} chunks · {embedded.toLocaleString()} embedded</div>
        </div>
      </div>

      <div className="card">
        <h2>Recent tickets</h2>
        <table>
          <thead><tr><th>ID</th><th>Status</th><th>Assignee</th><th>Title</th></tr></thead>
          <tbody>
            {tickets.map(t => (
              <tr key={t.id}>
                <td><Link to={`/tickets/${t.identifier}`}>{t.identifier}</Link></td>
                <td><span className={`chip ${statusClass(t.status)}`}>{t.status}</span></td>
                <td className="muted">{t.assignee_role || '—'}</td>
                <td>{t.title}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid grid-2">
        <div className="card">
          <h2>Agent cards</h2>
          {agents.map(a => (
            <div key={a.role} style={{ padding: '.5rem 0', borderBottom: '1px solid var(--border)' }}>
              <div className="row">
                <b>{a.role}</b>
                <span className="chip">{a.model}</span>
                <span className="chip">{a.transport}</span>
                <span className="muted small">max {a.max_turns} turns</span>
              </div>
              <div className="small muted">
                last activity: {a.last_activity || 'never'} · open: {a.active_tickets.length}
              </div>
            </div>
          ))}
        </div>
        <div className="card">
          <h2>Memory wings</h2>
          <table>
            <thead><tr><th>Tier</th><th>Wing</th><th>Count</th><th>Embedded</th></tr></thead>
            <tbody>
              {mem.wings.slice(0, 12).map((w: any, i: number) => (
                <tr key={i}>
                  <td>{w.tier}</td>
                  <td className="small">{w.wing}</td>
                  <td>{w.n}</td>
                  <td>{w.embedded}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function statusClass(s: string) {
  if (s === 'done') return 'ok';
  if (s === 'blocked' || s === 'cancelled') return 'err';
  if (s === 'in_progress' || s === 'in_review') return 'active';
  return '';
}
