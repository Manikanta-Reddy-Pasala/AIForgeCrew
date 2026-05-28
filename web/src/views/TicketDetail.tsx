import { useState } from 'react';
import { Link, useParams, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api, type WorkflowSpec } from '../api';
import { Icon } from '../icons';
import {
  statusClass, priorityClass, durationCell, durationTitle, relTime,
} from '../util';

const TRANSITIONS = ['todo', 'in_progress', 'in_review', 'qa', 'qa_failed', 'done', 'blocked', 'cancelled'];

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
          <RouteBadge t={t} onChanged={() => qc.invalidateQueries({ queryKey: ['ticket', id] })} />
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

          <AttachmentsBlock identifier={t.identifier} files={t.metadata?.attached_files} />

          {t.metadata?.enrichment && (
            <div className="card">
              <div className="card-header">
                <h2>Enrichment (IntentLayer)</h2>
              </div>
              <EnrichmentBlock enrichment={t.metadata.enrichment} />
            </div>
          )}

          {t.metadata?.runtime === 'aiforge_agents' && (
            <AiForgeAgentsBlock m={t.metadata} />
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
                  {e.metadata?.effective_provider && (
                    <span
                      className="chip sm"
                      title={
                        `configured: ${e.metadata.model_configured || '?'} / ${e.metadata.provider_configured || '?'}` +
                        (e.metadata.force_provider ? ` · forced: ${e.metadata.force_provider}` : '')
                      }
                      style={{
                        background: e.metadata.force_provider
                          ? 'rgba(180,120,40,0.18)' : 'rgba(80,140,200,0.18)',
                        marginLeft: 6,
                      }}
                    >
                      {e.metadata.model_configured
                        ? `${e.metadata.model_configured} · ${e.metadata.effective_provider}`
                        : e.metadata.effective_provider}
                    </span>
                  )}
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


function AiForgeAgentsBlock({ m }: { m: any }) {
  const plan = m.plan || {};
  const steps = plan.steps || [];
  const grounding = m.grounding || {};
  const verifier = m.verifier || {};
  const doer = m.doer || {};
  const validation = m.validation || {};
  const review = m.review || {};
  const learning = m.learning || {};
  const stages = m.stages_s || {};
  const allowed = m.allowed_files || [];
  const allowedCount = m.allowed_files_count || allowed.length;

  return (
    <div className="card">
      <div className="card-header">
        <h2>AIForge Agents pipeline</h2>
      </div>
      <div className="stack" style={{ gap: 12 }}>
        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          <span className="chip sm">verdict: <code>{verifier.verdict || '?'}</code></span>
          <span className="chip sm">grounded: <code>{String(grounding.resolved ?? '?')}</code></span>
          <span className="chip sm">validation: <code>{validation.decision || '?'}</code></span>
          <span className="chip sm">review: <code>{review.decision || '?'}</code></span>
          <span className="chip sm">outcome: <code>{learning.outcome || '?'}</code></span>
          <span className="chip sm">latency: <code>{m.latency_s || '?'}s</code></span>
        </div>

        {Object.keys(stages).length > 0 && (
          <details>
            <summary className="small"><strong>Stage timings (s)</strong></summary>
            <ul className="small mono" style={{ marginTop: 4 }}>
              {Object.entries(stages).map(([k, v]) => (
                <li key={k}>{k}: {String(v)}</li>
              ))}
            </ul>
          </details>
        )}

        {steps.length > 0 && (
          <details open>
            <summary className="small"><strong>Plan steps</strong> ({steps.length})</summary>
            <ol className="small mono" style={{ marginTop: 4 }}>
              {steps.map((s: any, k: number) => (
                <li key={k}>
                  <span className="chip sm" style={{ marginRight: 6 }}>{s.action}</span>
                  <code>{s.target}</code>
                  {s.expected && <div className="muted">↳ {s.expected}</div>}
                </li>
              ))}
            </ol>
          </details>
        )}

        {grounding.unresolved_refs?.length > 0 && (
          <details>
            <summary className="small"><strong>Unresolved refs</strong> ({grounding.unresolved_refs.length})</summary>
            <ul className="small mono" style={{ marginTop: 4 }}>
              {grounding.unresolved_refs.map((u: any, k: number) => (
                <li key={k}><code>{u.target}</code> — {u.reason} ({u.action})</li>
              ))}
            </ul>
          </details>
        )}

        {verifier.issues?.length > 0 && (
          <details>
            <summary className="small"><strong>Verifier issues</strong> ({verifier.issues.length})</summary>
            <ul className="small" style={{ marginTop: 4 }}>
              {verifier.issues.map((i: any, k: number) => (
                <li key={k}>
                  <strong>{i.kind}</strong> {i.step_id ? `(step ${i.step_id})` : ''}: {i.message}
                </li>
              ))}
            </ul>
          </details>
        )}

        {doer.problems?.length > 0 && (
          <details>
            <summary className="small"><strong>Doer detector hits</strong> ({doer.problems.length})</summary>
            <ul className="small mono" style={{ marginTop: 4 }}>
              {doer.problems.map((p: any, k: number) => (
                <li key={k}><strong>{p.mode}</strong> — {p.evidence}</li>
              ))}
            </ul>
          </details>
        )}

        {doer.udiff && (
          <details>
            <summary className="small"><strong>Doer udiff</strong> {doer.target ? `→ ${doer.target}` : ''}</summary>
            <pre className="small mono" style={{
              marginTop: 4, maxHeight: 360, overflow: 'auto',
              background: 'var(--bg-2)', padding: 8, borderRadius: 4,
            }}>{doer.udiff}</pre>
            {doer.artifact_path && (
              <div className="small muted">full patch: <code>{doer.artifact_path}</code></div>
            )}
            <div className="small muted">
              applied: <code>{String(doer.applied || false)}</code>
              {doer.applied_branch && <> · branch: <code>{doer.applied_branch}</code></>}
              {doer.apply_error && <> · err: <span style={{ color: 'tomato' }}>{doer.apply_error}</span></>}
            </div>
          </details>
        )}

        {(review.mr_title || review.mr_body) && (
          <details open>
            <summary className="small"><strong>Architect MR</strong> — {review.mr_title || '(untitled)'}</summary>
            {review.mr_url && (
              <div className="small">
                <a href={review.mr_url} target="_blank" rel="noopener">
                  {review.mr_url}
                </a>
              </div>
            )}
            {review.mr_body && (
              <pre className="small" style={{
                whiteSpace: 'pre-wrap', marginTop: 4,
                background: 'var(--bg-2)', padding: 8, borderRadius: 4,
              }}>{review.mr_body}</pre>
            )}
            {review.comments?.length > 0 && (
              <ul className="small" style={{ marginTop: 4 }}>
                {review.comments.map((c: string, k: number) => (
                  <li key={k}>{c}</li>
                ))}
              </ul>
            )}
          </details>
        )}

        {allowed.length > 0 && (
          <details>
            <summary className="small">
              <strong>Allowed files seeded</strong> (showing {allowed.length} of {allowedCount})
            </summary>
            <ul className="small mono" style={{ marginTop: 4 }}>
              {allowed.map((f: string, k: number) => <li key={k}>{f}</li>)}
            </ul>
          </details>
        )}
      </div>
    </div>
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


// ── Route badge ────────────────────────────────────────────────────
//
// Renders the current route (code task vs. workflow id) as a chip.
// Click → opens an inline override panel that lets the operator
// switch routes. Posts to PUT /api/tickets/{id}/route which records
// route_source='manual' so the change is auditable.

function RouteBadge({ t, onChanged }: { t: any; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [pickedKind, setPickedKind] = useState<'code' | 'workflow'>(
    t.route === 'workflow' ? 'workflow' : 'code',
  );
  const [pickedWorkflow, setPickedWorkflow] = useState<string>(
    t.route_workflow || '',
  );

  const { data: workflows = [] } = useQuery<WorkflowSpec[]>({
    queryKey: ['workflows'],
    queryFn: () => api.workflows(),
    staleTime: 60_000,
    enabled: open,
  });

  const isWorkflow = t.route === 'workflow' && t.route_workflow;
  const sourceLabel = t.route_source === 'manual' ? 'manual' : 'auto';
  const conf = typeof t.route_confidence === 'number'
    ? ` · ${t.route_confidence.toFixed(2)}` : '';

  async function save() {
    if (pickedKind === 'workflow' && !pickedWorkflow) {
      toast.error('Pick a workflow id'); return;
    }
    setPending(true);
    try {
      await api.setRoute(t.identifier, pickedKind,
        pickedKind === 'workflow' ? pickedWorkflow : undefined);
      toast.success(`Route → ${pickedKind === 'workflow' ? pickedWorkflow : 'code task'}`);
      setOpen(false);
      onChanged();
    } catch (e: any) {
      toast.error(`Override failed: ${e.message}`);
    } finally {
      setPending(false);
    }
  }

  return (
    <div style={{ position: 'relative' }}>
      <button
        className="chip"
        title={`Route source: ${sourceLabel}${conf}`}
        onClick={() => setOpen(o => !o)}
        style={{
          background: isWorkflow ? 'var(--accent-bg, #e8f4ff)' : 'transparent',
          border: '1px solid var(--border-1)',
          padding: '2px 8px',
          fontSize: 11,
          cursor: 'pointer',
        }}
      >
        {isWorkflow ? '⚙' : '📝'}{' '}
        {isWorkflow ? t.route_workflow : 'code task'}
        <span style={{ opacity: 0.6, marginLeft: 6 }}>[{sourceLabel}]</span>
      </button>
      {open && (
        <div className="card" style={{
          position: 'absolute', top: '100%', right: 0, marginTop: 6,
          padding: 12, minWidth: 320, zIndex: 50,
        }}>
          <div style={{ marginBottom: 8 }}><strong>Override route</strong></div>
          <div className="row" style={{ gap: 12, marginBottom: 8 }}>
            <label className="row" style={{ gap: 4 }}>
              <input type="radio"
                checked={pickedKind === 'code'}
                onChange={() => setPickedKind('code')} />
              Code task
            </label>
            <label className="row" style={{ gap: 4 }}>
              <input type="radio"
                checked={pickedKind === 'workflow'}
                onChange={() => setPickedKind('workflow')} />
              Workflow
            </label>
          </div>
          {pickedKind === 'workflow' && (
            <select
              value={pickedWorkflow}
              onChange={e => setPickedWorkflow(e.target.value)}
              style={{ width: '100%', marginBottom: 8 }}
            >
              <option value="">— pick a workflow —</option>
              {workflows.map(w => (
                <option key={w.id} value={w.id}>{w.label}</option>
              ))}
            </select>
          )}
          <div className="row" style={{ justifyContent: 'flex-end', gap: 8 }}>
            <button className="ghost" onClick={() => setOpen(false)}>Cancel</button>
            <button onClick={save} disabled={pending}>Save</button>
          </div>
        </div>
      )}
    </div>
  );
}

type AttachedFile = { name: string; path?: string; size?: number };

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|bmp)$/i;

function AttachmentsBlock({
  identifier, files,
}: { identifier: string; files?: AttachedFile[] }) {
  if (!files || !Array.isArray(files) || files.length === 0) return null;
  const fmt = (n?: number) => {
    if (!n && n !== 0) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  };
  return (
    <div className="card">
      <div className="card-header"><h2>Attachments ({files.length})</h2></div>
      <div className="stack" style={{ gap: 12 }}>
        {files.map((f, i) => {
          const url = `/files/${encodeURIComponent(identifier)}/${encodeURIComponent(f.name)}`;
          const isImage = IMAGE_EXT.test(f.name);
          return (
            <div key={i} className="row" style={{ gap: 12, alignItems: 'flex-start' }}>
              {isImage ? (
                <a href={url} target="_blank" rel="noopener">
                  <img
                    src={url}
                    alt={f.name}
                    style={{
                      maxWidth: 160, maxHeight: 120,
                      border: '1px solid var(--border-1)', borderRadius: 4,
                      objectFit: 'cover',
                    }}
                  />
                </a>
              ) : (
                <div style={{
                  width: 64, height: 64, display: 'flex',
                  alignItems: 'center', justifyContent: 'center',
                  border: '1px solid var(--border-1)', borderRadius: 4,
                  fontSize: 12, color: 'var(--muted)',
                }}>
                  {(f.name.split('.').pop() || 'file').slice(0, 4).toUpperCase()}
                </div>
              )}
              <div className="stack" style={{ gap: 4, minWidth: 0, flex: 1 }}>
                <a href={url} target="_blank" rel="noopener" style={{ wordBreak: 'break-all' }}>
                  {f.name}
                </a>
                <div className="muted small">{fmt(f.size)}</div>
                {f.path && (
                  <code className="mono small muted" style={{ wordBreak: 'break-all' }}>
                    {f.path}
                  </code>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
