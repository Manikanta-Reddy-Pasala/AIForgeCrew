import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';

const ROLES = ['', 'supervisor', 'planner', 'doer', 'feedback', 'learner'];
const STATUSES = ['', 'todo', 'in_progress', 'in_review', 'done', 'blocked', 'cancelled'];

export default function Tickets() {
  const [rows, setRows] = useState<any[]>([]);
  const [role, setRole] = useState('');
  const [status, setStatus] = useState('');
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState({
    title: '', body: '', assignee_role: 'planner', priority: 'medium',
  });

  async function load() {
    const qs: Record<string, string> = {};
    if (role) qs.role = role;
    if (status) qs.status = status;
    setRows(await api.tickets(qs));
  }

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [role, status]);

  async function submit() {
    await api.create(draft);
    setDraft({ title: '', body: '', assignee_role: 'planner', priority: 'medium' });
    setCreating(false);
    load();
  }

  return (
    <>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: '.75rem' }}>
        <h1>Tickets</h1>
        <button onClick={() => setCreating(!creating)}>
          {creating ? 'Cancel' : 'New ticket'}
        </button>
      </div>

      {creating && (
        <div className="card">
          <h2>New ticket</h2>
          <div className="row">
            <input placeholder="title" value={draft.title}
                   onChange={e => setDraft({ ...draft, title: e.target.value })}
                   style={{ flex: 1 }} />
            <select value={draft.assignee_role}
                    onChange={e => setDraft({ ...draft, assignee_role: e.target.value })}>
              {ROLES.filter(Boolean).map(r => <option key={r} value={r}>{r}</option>)}
            </select>
            <select value={draft.priority}
                    onChange={e => setDraft({ ...draft, priority: e.target.value })}>
              {['low', 'medium', 'high', 'urgent'].map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <textarea placeholder="body" rows={5} value={draft.body}
                    onChange={e => setDraft({ ...draft, body: e.target.value })}
                    style={{ width: '100%', marginTop: '.5rem' }} />
          <div style={{ marginTop: '.5rem' }}>
            <button onClick={submit} disabled={!draft.title}>Create</button>
          </div>
        </div>
      )}

      <div className="card">
        <div className="row" style={{ marginBottom: '.5rem' }}>
          <label>Role:{' '}
            <select value={role} onChange={e => setRole(e.target.value)}>
              {ROLES.map(r => <option key={r} value={r}>{r || 'any'}</option>)}
            </select>
          </label>
          <label>Status:{' '}
            <select value={status} onChange={e => setStatus(e.target.value)}>
              {STATUSES.map(s => <option key={s} value={s}>{s || 'any'}</option>)}
            </select>
          </label>
          <span className="muted small">{rows.length} shown</span>
        </div>
        <table>
          <thead><tr><th>ID</th><th>Status</th><th>Priority</th><th>Assignee</th><th>Title</th><th>Duration</th><th>Updated</th></tr></thead>
          <tbody>
            {rows.map(t => (
              <tr key={t.id}>
                <td><Link to={`/tickets/${t.identifier}`}>{t.identifier}</Link></td>
                <td><span className={`chip ${statusClass(t.status)}`}>{t.status}</span></td>
                <td className="small muted">{t.priority}</td>
                <td className="muted small">{t.assignee_role || '—'}</td>
                <td>{t.title}</td>
                <td className="small muted" title={durationTitle(t)}>{durationCell(t)}</td>
                <td className="small muted">{t.updated_at?.slice(0, 19).replace('T', ' ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
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

const TERMINAL = new Set(['done', 'cancelled']);

export function formatDuration(sec: number | null | undefined): string {
  if (sec == null || !isFinite(sec) || sec < 0) return '—';
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), rs = s % 60;
  if (m < 60) return rs ? `${m}m ${rs}s` : `${m}m`;
  const h = Math.floor(m / 60), rm = m % 60;
  if (h < 24) return rm ? `${h}h ${rm}m` : `${h}h`;
  const d = Math.floor(h / 24), rh = h % 24;
  return rh ? `${d}d ${rh}h` : `${d}d`;
}

export function durationCell(t: any): string {
  if (!t.started_at) return t.status === 'todo' ? '—' : '…';
  const live = !TERMINAL.has(t.status);
  return live ? `${formatDuration(t.duration_s)} ⏱` : formatDuration(t.duration_s);
}

export function durationTitle(t: any): string {
  if (!t.started_at) return 'never entered in_progress';
  const live = !TERMINAL.has(t.status);
  const base = `started ${t.started_at.slice(0, 19).replace('T', ' ')}`;
  return live ? `${base} (running)` : `${base} → ${t.completed_at?.slice(0, 19).replace('T', ' ') || 'end'}`;
}
