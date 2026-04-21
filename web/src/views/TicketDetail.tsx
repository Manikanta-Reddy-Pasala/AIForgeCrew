import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api';
import { durationCell, durationTitle } from './Tickets';

export default function TicketDetail() {
  const { id = '' } = useParams();
  const [data, setData] = useState<any>(null);
  const [comment, setComment] = useState('');

  async function load() { setData(await api.ticket(id)); }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [id]);

  if (!data) return <p className="muted">loading…</p>;
  const t = data.ticket;

  async function postComment() {
    if (!comment.trim()) return;
    await api.comment(id, comment);
    setComment('');
    load();
  }

  async function setStatus(s: string) {
    await api.patch(id, { status: s });
    load();
  }

  return (
    <>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <h1>{t.identifier} · {t.title}</h1>
        <span className={`chip ${statusClass(t.status)}`}>{t.status}</span>
      </div>
      <div className="row small muted" style={{ marginBottom: '1rem' }}>
        <span>assignee: {t.assignee_role || '—'}</span>
        <span>priority: {t.priority}</span>
        <span title={durationTitle(t)}>duration: {durationCell(t)}</span>
        {t.branch && <span>branch: <code>{t.branch}</code></span>}
        {t.parent_id && <span>parent: #{t.parent_id}</span>}
      </div>

      <div className="card">
        <h2>Body</h2>
        <pre>{t.body || '(empty)'}</pre>
      </div>

      {data.children?.length > 0 && (
        <div className="card">
          <h2>Children ({data.children.length})</h2>
          <table>
            <thead><tr><th>ID</th><th>Status</th><th>Assignee</th><th>Title</th><th>Duration</th></tr></thead>
            <tbody>
              {data.children.map((c: any) => (
                <tr key={c.id}>
                  <td><Link to={`/tickets/${c.identifier}`}>{c.identifier}</Link></td>
                  <td><span className={`chip ${statusClass(c.status)}`}>{c.status}</span></td>
                  <td className="muted small">{c.assignee_role || '—'}</td>
                  <td>{c.title}</td>
                  <td className="small muted" title={durationTitle(c)}>{durationCell(c)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card">
        <h2>Events ({data.events.length})</h2>
        <div className="event-log">
          {data.events.map((e: any) => (
            <div key={e.id} className="event-row">
              <span className="ts">{e.created_at?.slice(11, 19)}</span>
              <span className="role">{e.agent_role || 'system'}</span>
              <span className="kind">{e.kind}</span>
              <span>{(e.body || '').slice(0, 500)}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h2>Add comment</h2>
        <textarea rows={4} value={comment} onChange={e => setComment(e.target.value)}
                  style={{ width: '100%' }} placeholder="human comment…" />
        <div className="row" style={{ marginTop: '.5rem' }}>
          <button onClick={postComment}>Post</button>
          <div style={{ flex: 1 }} />
          {['todo', 'in_progress', 'in_review', 'done', 'blocked', 'cancelled']
            .filter(s => s !== t.status)
            .map(s => (
              <button key={s} className="ghost" onClick={() => setStatus(s)}>
                → {s}
              </button>
            ))}
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
