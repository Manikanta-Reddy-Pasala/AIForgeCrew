import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api } from '../api';
import { Icon } from '../icons';
import {
  statusClass, priorityClass, durationCell, durationTitle, relTime,
} from '../util';

const ROLES = ['', 'supervisor', 'planner', 'doer', 'feedback', 'learner'];
const STATUSES = ['', 'todo', 'in_progress', 'in_review', 'done', 'blocked', 'cancelled'];
const PRIORITIES = ['low', 'medium', 'high', 'urgent'];

export default function Tickets() {
  const qc = useQueryClient();
  const [role, setRole] = useState('');
  const [status, setStatus] = useState('');
  const [search, setSearch] = useState('');
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState({
    title: '', body: '', assignee_role: 'planner', priority: 'medium',
  });

  const { data: rows = [], isLoading } = useQuery({
    queryKey: ['tickets', role, status],
    queryFn: () => {
      const qs: Record<string, string> = {};
      if (role) qs.role = role;
      if (status) qs.status = status;
      return api.tickets(qs);
    },
  });

  const visible = rows.filter((t: any) => {
    if (!search.trim()) return true;
    const needle = search.toLowerCase();
    return `${t.identifier} ${t.title}`.toLowerCase().includes(needle);
  });

  async function submit() {
    if (!draft.title.trim()) return;
    try {
      await api.create(draft);
      toast.success(`Created: ${draft.title}`);
      setDraft({ title: '', body: '', assignee_role: 'planner', priority: 'medium' });
      setCreating(false);
      qc.invalidateQueries({ queryKey: ['tickets'] });
    } catch (e: any) {
      toast.error(`Create failed: ${e.message}`);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Tickets</h1>
          <div className="subtitle">
            {visible.length.toLocaleString()} shown
            {rows.length !== visible.length ? ` of ${rows.length.toLocaleString()}` : ''}
          </div>
        </div>
        <div className="row">
          <button onClick={() => setCreating(c => !c)}>
            {creating ? <><Icon.X size={14} /> Cancel</> : <><Icon.Plus size={14} /> New ticket</>}
          </button>
        </div>
      </div>

      {creating && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-header"><h2>New ticket</h2></div>
          <div className="stack">
            <label className="field">
              Title
              <input
                placeholder="Short, descriptive summary"
                value={draft.title}
                onChange={e => setDraft({ ...draft, title: e.target.value })}
                autoFocus
              />
            </label>
            <div className="grid grid-3">
              <label className="field">
                Assignee
                <select value={draft.assignee_role} onChange={e => setDraft({ ...draft, assignee_role: e.target.value })}>
                  {ROLES.filter(Boolean).map(r => <option key={r} value={r}>{r}</option>)}
                </select>
              </label>
              <label className="field">
                Priority
                <select value={draft.priority} onChange={e => setDraft({ ...draft, priority: e.target.value })}>
                  {PRIORITIES.map(p => <option key={p} value={p}>{p}</option>)}
                </select>
              </label>
              <div />
            </div>
            <label className="field">
              Body
              <textarea
                rows={6}
                placeholder="Context, acceptance, hints…"
                value={draft.body}
                onChange={e => setDraft({ ...draft, body: e.target.value })}
              />
            </label>
            <div className="row" style={{ justifyContent: 'flex-end' }}>
              <button className="ghost" onClick={() => setCreating(false)}>Cancel</button>
              <button onClick={submit} disabled={!draft.title.trim()}>
                <Icon.Check size={14} /> Create ticket
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="filter-bar">
        <div className="input-search" style={{ flex: 1, minWidth: 220 }}>
          <Icon.Search size={14} />
          <input placeholder="search id or title…" value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <label className="field">
          Role
          <select value={role} onChange={e => setRole(e.target.value)}>
            {ROLES.map(r => <option key={r} value={r}>{r || 'any'}</option>)}
          </select>
        </label>
        <label className="field">
          Status
          <select value={status} onChange={e => setStatus(e.target.value)}>
            {STATUSES.map(s => <option key={s} value={s}>{s || 'any'}</option>)}
          </select>
        </label>
      </div>

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        {isLoading ? (
          <div className="empty"><div className="skeleton" style={{ width: 200, height: 16 }} /></div>
        ) : visible.length === 0 ? (
          <div className="empty">
            <div className="empty-icon"><Icon.Filter size={18} /></div>
            <div style={{ color: 'var(--fg-0)', fontWeight: 500 }}>No tickets match</div>
            <div>Adjust filters or create a new one.</div>
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Status</th>
                <th>Priority</th>
                <th>Assignee</th>
                <th>Title</th>
                <th>Duration</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((t: any) => (
                <tr key={t.id}>
                  <td><Link to={`/tickets/${t.identifier}`} className="identifier-badge">{t.identifier}</Link></td>
                  <td><span className={`chip ${statusClass(t.status)}`}>{t.status.replace('_', ' ')}</span></td>
                  <td><span className={`chip ${priorityClass(t.priority)}`}>{t.priority}</span></td>
                  <td className="small muted">{t.assignee_role || '—'}</td>
                  <td style={{ maxWidth: 420, color: 'var(--fg-0)' }}>
                    <Link to={`/tickets/${t.identifier}`} style={{ color: 'inherit' }}>{t.title}</Link>
                  </td>
                  <td className="small mono muted nowrap" title={durationTitle(t)}>{durationCell(t)}</td>
                  <td className="small muted nowrap" title={t.updated_at}>{relTime(t.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
