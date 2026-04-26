import { useState } from 'react';
import { Link, useParams, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api } from '../api';
import { Icon } from '../icons';
import {
  statusClass, priorityClass, durationCell, durationTitle, relTime,
} from '../util';

const TRANSITIONS = ['todo', 'in_progress', 'in_review', 'done', 'blocked', 'cancelled'];

export default function TicketDetail() {
  const { id = '' } = useParams();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [comment, setComment] = useState('');

  const { data, isLoading } = useQuery({
    queryKey: ['ticket', id],
    queryFn: () => api.ticket(id),
    enabled: !!id,
  });

  if (isLoading || !data) {
    return (
      <div className="stack">
        <div className="skeleton" style={{ width: 220, height: 24 }} />
        <div className="skeleton" style={{ width: '100%', height: 120 }} />
      </div>
    );
  }

  const t = data.ticket;

  async function postComment() {
    if (!comment.trim()) return;
    try {
      await api.comment(id, comment);
      toast.success('Comment posted');
      setComment('');
      qc.invalidateQueries({ queryKey: ['ticket', id] });
    } catch (e: any) { toast.error(e.message); }
  }

  async function setStatus(s: string) {
    try {
      await api.patch(id, { status: s });
      toast.success(`Moved to ${s.replace('_', ' ')}`);
      qc.invalidateQueries({ queryKey: ['ticket', id] });
      qc.invalidateQueries({ queryKey: ['tickets'] });
    } catch (e: any) { toast.error(e.message); }
  }

  async function deleteTicket() {
    if (!window.confirm(`Delete ${t.identifier} permanently? Events, child tickets, and PRs are NOT removed.`)) return;
    try {
      await api.delete(id);
      toast.success(`Deleted ${t.identifier}`);
      qc.invalidateQueries({ queryKey: ['tickets'] });
      navigate('/');
    } catch (e: any) { toast.error(e.message); }
  }

  return (
    <>
      <div className="detail-header">
        <div className="detail-title-row">
          <span className="identifier-badge mono" style={{ fontSize: 13 }}>{t.identifier}</span>
          <h1 style={{ flex: 1 }}>{t.title}</h1>
          <span className={`chip ${statusClass(t.status)}`}>{t.status.replace('_', ' ')}</span>
          <span className={`chip ${priorityClass(t.priority)}`}>{t.priority}</span>
        </div>
        <div className="detail-meta">
          <span title="last active agent stage"><strong>Active</strong> {t.active_role || t.assignee_role || '—'}</span>
          <span title={durationTitle(t)}><strong>Duration</strong> {durationCell(t)}</span>
          <span><strong>Updated</strong> {relTime(t.updated_at)}</span>
          {t.branch && <span><strong>Branch</strong> <code>{t.branch}</code></span>}
          {t.parent_id && <span><strong>Parent</strong> #{t.parent_id}</span>}
        </div>
      </div>

      <div className="row" style={{ gap: 8, marginBottom: 12 }}>
        <Link to={`/trace/${id}`} className="ghost sm" style={{
          padding: '4px 10px', border: '1px solid var(--border-1)', borderRadius: 4,
        }}>
          <Icon.Logs size={14} /> Live trace
        </Link>
        <Link to={`/llm-trace/${id}`} className="ghost sm" style={{
          padding: '4px 10px', border: '1px solid var(--border-1)', borderRadius: 4,
        }}>
          <Icon.Chat size={14} /> LLM history
        </Link>
        <Link to={`/workflow?ticket=${id}`} className="ghost sm" style={{
          padding: '4px 10px', border: '1px solid var(--border-1)', borderRadius: 4,
        }}>
          <Icon.GitBranch size={14} /> Workflow overlay
        </Link>
      </div>

      <div className="grid grid-2" style={{ alignItems: 'start' }}>
        <div className="stack">
          <div className="card">
            <div className="card-header"><h2>Body</h2></div>
            {t.body
              ? <pre style={{ whiteSpace: 'pre-wrap' }}>{t.body}</pre>
              : <div className="muted small">(empty)</div>}
          </div>

          {t.metadata?.enrichment && (
            <div className="card">
              <div className="card-header">
                <h2>Enrichment (IntentLayer)</h2>
              </div>
              <EnrichmentBlock enrichment={t.metadata.enrichment} />
            </div>
          )}

          {data.children?.length > 0 && (
            <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
              <div className="card-header" style={{ padding: '16px 20px 0' }}>
                <h2>Children ({data.children.length})</h2>
              </div>
              <table>
                <thead><tr><th>ID</th><th>Status</th><th>Assignee</th><th>Title</th><th>Duration</th></tr></thead>
                <tbody>
                  {data.children.map((c: any) => (
                    <tr key={c.id}>
                      <td><Link to={`/tickets/${c.identifier}`} className="identifier-badge">{c.identifier}</Link></td>
                      <td><span className={`chip ${statusClass(c.status)}`}>{c.status.replace('_', ' ')}</span></td>
                      <td className="muted small">{c.assignee_role || '—'}</td>
                      <td>{c.title}</td>
                      <td className="small mono muted" title={durationTitle(c)}>{durationCell(c)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="card">
            <div className="card-header"><h2>Events ({data.events.length})</h2></div>
            <div className="event-log">
              {data.events.length === 0 && <div className="empty">No events yet.</div>}
              {data.events.map((e: any) => (
                <div key={e.id} className="event-row">
                  <span className="ts">{(e.created_at || '').slice(11, 19)}</span>
                  <span className="role">{e.agent_role || 'system'}</span>
                  <span className="kind">{e.kind}</span>
                  {e.body && <div className="event-body">{String(e.body).slice(0, 800)}</div>}
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="stack">
          <div className="card">
            <div className="card-header"><h2>Transition</h2></div>
            <div className="row" style={{ gap: 8, alignItems: 'center' }}>
              <label className="small muted">Move to</label>
              <select
                value={t.status}
                onChange={e => {
                  const s = e.target.value;
                  if (s !== t.status) setStatus(s);
                }}
                style={{ minWidth: 160 }}
              >
                {TRANSITIONS.map(s => (
                  <option key={s} value={s}>{s.replace('_', ' ')}</option>
                ))}
              </select>
              <button className="ghost sm danger" onClick={deleteTicket} style={{ marginLeft: 'auto' }}>
                <Icon.Trash size={14} /> Delete
              </button>
            </div>
          </div>

          <div className="card">
            <div className="card-header"><h2>Comment</h2></div>
            <div className="stack">
              <textarea
                rows={4}
                value={comment}
                onChange={e => setComment(e.target.value)}
                placeholder="Leave a comment for the crew…"
              />
              <div className="row" style={{ justifyContent: 'flex-end' }}>
                <button onClick={postComment} disabled={!comment.trim()}>
                  <Icon.Send size={14} /> Post comment
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}


function EnrichmentBlock({ enrichment }: { enrichment: any }) {
  const i = enrichment.intent || {};
  const focal = enrichment.focal_files || [];
  const ref = enrichment.reference_files || [];
  const sims = enrichment.similar_tickets || [];
  const t3 = enrichment.t3_recipes || [];
  const cmds = enrichment.commands || {};
  const sources = enrichment.sources_used || [];
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
        <span className="chip sm" style={{ fontWeight: 600 }}>{i.action || '?'}</span>
        <span className="chip sm">entity: <code>{i.entity || '?'}</code></span>
        {i.reference_pattern && (
          <span className="chip sm">ref: <code>{i.reference_pattern}</code></span>
        )}
        {enrichment.repo && (
          <span className="chip sm">repo: <code>{enrichment.repo}</code></span>
        )}
      </div>
      {i.keywords?.length > 0 && (
        <div className="small muted">
          keywords: {i.keywords.join(', ')}
        </div>
      )}
      {focal.length > 0 && (
        <details>
          <summary className="small"><strong>Focal files</strong> ({focal.length})</summary>
          <ul className="small mono" style={{ marginTop: 4 }}>
            {focal.map((f: string, k: number) => <li key={k}>{f}</li>)}
          </ul>
        </details>
      )}
      {ref.length > 0 && (
        <details>
          <summary className="small"><strong>Reference files</strong> ({ref.length})</summary>
          <ul className="small mono" style={{ marginTop: 4 }}>
            {ref.map((f: string, k: number) => <li key={k}>{f}</li>)}
          </ul>
        </details>
      )}
      {sims.length > 0 && (
        <details>
          <summary className="small"><strong>Similar past tickets</strong> ({sims.length})</summary>
          <ul className="small" style={{ marginTop: 4 }}>
            {sims.map((s: any, k: number) => (
              <li key={k}>
                <Link to={`/tickets/${s.identifier}`} className="identifier-badge">
                  {s.identifier}
                </Link>
                {' '}<span className="muted">({s.status})</span> — {s.title?.slice(0, 90)}
              </li>
            ))}
          </ul>
        </details>
      )}
      {t3.length > 0 && (
        <details>
          <summary className="small"><strong>T3 recipes</strong> ({t3.length})</summary>
          <ul className="small" style={{ marginTop: 4 }}>
            {t3.map((r: string, k: number) => <li key={k}>{r.slice(0, 280)}</li>)}
          </ul>
        </details>
      )}
      {Object.keys(cmds).length > 0 && (
        <details>
          <summary className="small"><strong>Build commands</strong></summary>
          <ul className="small mono" style={{ marginTop: 4 }}>
            {Object.entries(cmds).map(([k, v]) => (
              <li key={k}><strong>{k}:</strong> {String(v)}</li>
            ))}
          </ul>
        </details>
      )}
      <div className="small muted">sources: {sources.join(', ')}</div>
    </div>
  );
}
