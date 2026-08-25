import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api, type RoutePreview, type WorkflowSpec } from '../api';
import { Icon } from '../icons';
import {
  statusClass, priorityClass, durationCell, durationTitle, relTime,
} from '../util';
import { DropZone, readAsBase64, MAX_FILE_BYTES, formatBytes } from '../components/FileUpload';

const ROLES = ['', 'supervisor', 'planner', 'doer', 'feedback', 'learner'];
const STATUSES = ['', 'todo', 'in_progress', 'in_review', 'qa', 'qa_failed', 'done', 'blocked', 'cancelled'];
const PRIORITIES = ['low', 'medium', 'high', 'urgent'];

type RouteMode = 'auto' | 'code' | 'workflow';

interface AttachedFile {
  name: string;
  size: number;
  content_b64: string;
}

interface Draft {
  title: string;
  body: string;
  assignee_role: string;
  priority: string;
  project: string;                // AFM repo name / target codeRepo dir
  route_mode: RouteMode;          // local UI state — translated on submit
  route_workflow: string;         // selected workflow id (when mode='workflow')
  attachments: string;            // comma-separated attachment role names
  attached_files: AttachedFile[]; // operator-uploaded files; materialized into the worktree for the Doer
  external_refs: string;          // newline-separated URLs / file paths (gap-9)
  scope_allowlist_globs: string;  // newline-separated globs (gap-C6)
  deploy_target: string;          // 'none' | 'qa' | 'prod' — arms auto-merge + wait
}

const FRESH_DRAFT: Draft = {
  title: '', body: '', assignee_role: 'planner', priority: 'medium',
  project: '',
  route_mode: 'auto', route_workflow: '', attachments: '',
  attached_files: [],
  external_refs: '',
  scope_allowlist_globs: '',
  deploy_target: 'none',
};

export default function Tickets() {
  const qc = useQueryClient();
  const [role, setRole] = useState('');
  const [status, setStatus] = useState('');
  const [search, setSearch] = useState('');
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<Draft>(FRESH_DRAFT);

  // Workflow registry — pulled once, drives the override dropdown.
  const { data: workflows = [] } = useQuery<WorkflowSpec[]>({
    queryKey: ['workflows'],
    queryFn: () => api.workflows(),
    staleTime: 60_000,
  });

  // Live route preview — debounced 350ms on body/attachments change.
  const [preview, setPreview] = useState<RoutePreview | null>(null);
  const previewKey = useMemo(
    () => JSON.stringify({
      body: draft.body, title: draft.title,
      atts: draft.attachments,
    }),
    [draft.body, draft.title, draft.attachments],
  );
  useEffect(() => {
    if (!creating || !draft.body.trim()) { setPreview(null); return; }
    const handle = window.setTimeout(async () => {
      try {
        const atts = draft.attachments
          .split(',').map(s => s.trim()).filter(Boolean);
        const r = await api.workflowPreview(draft.body, {
          title: draft.title, attachments: atts,
        });
        setPreview(r);
      } catch { setPreview(null); }
    }, 350);
    return () => window.clearTimeout(handle);
  }, [previewKey, creating]);

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
    const atts = draft.attachments
      .split(',').map(s => s.trim()).filter(Boolean);
    // Translate UI mode → API payload.
    //   'auto'     → omit route fields, server detector picks
    //   'code'     → explicit code task, skips workflow detection
    //   'workflow' → must include route_workflow id
    const payload: Record<string, any> = {
      title: draft.title, body: draft.body,
      assignee_role: draft.assignee_role, priority: draft.priority,
      attachments: atts,
      // attached_files only sent when actually present; the runner
      // materializes them into the worktree so the Doer can file_read them.
      attached_files: draft.attached_files.map(f => ({
        name: f.name, content_b64: f.content_b64,
      })),
    };
    // Optional target repo (AFM Repo.name / WORKTREE_ROOT directory).
    const project = draft.project.trim();
    if (project) payload.project = project;
    // Deploy target — only send when non-default. Server normalizes
    // to {none, qa, prod}; anything else falls back to 'none'.
    if (draft.deploy_target && draft.deploy_target !== 'none') {
      payload.deploy_target = draft.deploy_target;
    }
    // Optional URL / path list parsed from textarea (one per line).
    // Wires to adk_runner._ingest_ticket_external_refs (gap-9).
    const extRefs = draft.external_refs
      .split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    // Optional glob allowlist for the diff-scope guard (gap-C6).
    const scopeGlobs = draft.scope_allowlist_globs
      .split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    if (extRefs.length || scopeGlobs.length) {
      payload.metadata = {
        ...(extRefs.length ? { external_refs: extRefs } : {}),
        ...(scopeGlobs.length ? { scope_allowlist_globs: scopeGlobs } : {}),
      };
    }
    if (draft.route_mode === 'code') {
      payload.route = 'code';
    } else if (draft.route_mode === 'workflow') {
      if (!draft.route_workflow) {
        toast.error('Pick a workflow id');
        return;
      }
      payload.route = 'workflow';
      payload.route_workflow = draft.route_workflow;
    }
    try {
      await api.create(payload);
      toast.success(`Created: ${draft.title}`);
      setDraft(FRESH_DRAFT);
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
          <button type="button" onClick={() => setCreating(c => !c)}>
            {creating ? <><Icon.X size={14} /> Cancel</> : <><Icon.Plus size={14} /> New ticket</>}
          </button>
          <button type="button"
            className="danger"
            title="Delete every ticket and reset the ONE-<n> counter. Memory, skills, workflows and rules are NOT touched."
            onClick={async () => {
              if (!window.confirm('Delete ALL tickets and reset the sequence (ONE-100…)? This removes every ticket + its events. Memory, skills, workflows and rules are kept.')) return;
              try {
                const r = await api.resetTickets();
                toast.success(`Deleted ${r.deleted} tickets · sequence reset`);
                qc.invalidateQueries({ queryKey: ['tickets'] });
              } catch (e: any) { toast.error(e.message); }
            }}
          >
            <Icon.Trash size={14} /> Delete all & reset
          </button>
        </div>
      </div>

      {creating && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-header"><h2>New ticket</h2></div>
          <div className="stack">
            <label className="field">
              Title{' '}
              <input
                placeholder="Short, descriptive summary"
                value={draft.title}
                onChange={e => setDraft({ ...draft, title: e.target.value })}
                autoFocus
              />
            </label>
            <div className="grid grid-3">
              <label className="field">
                Assignee{' '}
                <select value={draft.assignee_role} onChange={e => setDraft({ ...draft, assignee_role: e.target.value })}>
                  {ROLES.filter(Boolean).map(r => <option key={r} value={r}>{r}</option>)}
                </select>
              </label>
              <label className="field">
                Priority{' '}
                <select value={draft.priority} onChange={e => setDraft({ ...draft, priority: e.target.value })}>
                  {PRIORITIES.map(p => <option key={p} value={p}>{p}</option>)}
                </select>
              </label>
              <label className="field">
                Project{' '}
                <input
                  placeholder="e.g. TallyConnector"
                  value={draft.project}
                  onChange={e => setDraft({ ...draft, project: e.target.value })}
                />
              </label>
              <label className="field">
                Deploy + test{' '}
                <select
                  value={draft.deploy_target}
                  onChange={e => setDraft({ ...draft, deploy_target: e.target.value })}
                  title="When 'qa' or 'prod', the live_verifier will auto-merge the PR, wait for the deploy to land, then verify on that environment."
                >
                  <option value="none">none — open PR only</option>
                  <option value="qa">qa — auto-merge + deploy + verify</option>
                  <option value="prod">prod — auto-merge + tag + deploy + verify</option>
                </select>
              </label>
            </div>
            <label className="field">
              Body{' '}
              <textarea
                rows={6}
                placeholder="Context, acceptance, hints…"
                value={draft.body}
                onChange={e => setDraft({ ...draft, body: e.target.value })}
              />
            </label>
            <label className="field">
              External references{' '}
              <span style={{ opacity: 0.6, fontSize: 12 }}>
                (one URL or file path per line — auto-ingested into AFM
                so the Doer's memory hits include their content)
              </span>
              <textarea
                rows={3}
                placeholder={'https://example.com/spec.md\nhttps://api.linear.app/...\n/srv/docs/runbook.md'}
                value={draft.external_refs}
                onChange={e => setDraft({ ...draft, external_refs: e.target.value })}
              />
            </label>
            <label className="field">
              Scope allowlist{' '}
              <span style={{ opacity: 0.6, fontSize: 12 }}>
                (one glob per line — Doer edits outside these globs
                are refused at the tool boundary)
              </span>
              <textarea
                rows={2}
                placeholder={'src/**\ntests/**\n!**/*.lock'}
                value={draft.scope_allowlist_globs}
                onChange={e => setDraft({ ...draft, scope_allowlist_globs: e.target.value })}
              />
            </label>

            <label className="field">
              Attachments (comma-separated role names){' '}
              <input
                placeholder="e.g. tally, oneshell"
                value={draft.attachments}
                onChange={e => setDraft({ ...draft, attachments: e.target.value })}
              />
            </label>

            <div className="field">
              <div style={{ marginBottom: 6 }}>
                Attached files{' '}
                <span style={{ opacity: 0.6, fontSize: 12 }}>
                  (drag &amp; drop or click — 5 MB cap per file; files are
                  materialized into the worktree for the Doer to read)
                </span>
              </div>
              <DropZone
                onFiles={async (picked) => {
                  const accepted: AttachedFile[] = [];
                  for (const f of picked) {
                    if (f.size > MAX_FILE_BYTES) {
                      toast.error(`${f.name} is ${formatBytes(f.size)} — over 5MB cap, skipping`);
                      continue;
                    }
                    try {
                      const b64 = await readAsBase64(f);
                      accepted.push({ name: f.name, size: f.size, content_b64: b64 });
                    } catch (err: any) {
                      toast.error(`Couldn't read ${f.name}: ${err.message || err}`);
                    }
                  }
                  if (accepted.length) {
                    setDraft(d => ({
                      ...d,
                      attached_files: [...d.attached_files, ...accepted],
                    }));
                    toast.success(
                      `Added ${accepted.length} file${accepted.length > 1 ? 's' : ''} — ` +
                      `materialized into the worktree for the Doer`,
                    );
                  }
                }}
              />
              {(draft.attached_files?.length ?? 0) > 0 && (
                <div className="row" style={{
                  gap: 6, flexWrap: 'wrap', marginTop: 8,
                  padding: 8,
                  border: '1px solid var(--border-1)',
                  borderRadius: 4,
                  background: 'var(--bg-1)',
                }}>
                  <strong style={{ marginRight: 4 }}>
                    📎 Attachments:
                  </strong>
                  {draft.attached_files.map((f, i) => (
                    <span key={i} style={{
                      padding: '2px 6px',
                      background: 'var(--bg-2)',
                      borderRadius: 3,
                      fontSize: 12,
                    }}>
                      📎 {f.name} ({formatBytes(f.size)}){' '}
                      <button type="button"
                        className="ghost"
                        style={{ padding: 0, marginLeft: 2, fontSize: 12 }}
                        onClick={() => setDraft({
                          ...draft,
                          attached_files: draft.attached_files.filter((_, j) => j !== i),
                        })}
                      >×</button>
                    </span>
                  ))}
                </div>
              )}
            </div>

            <div className="field">
              <div style={{ marginBottom: 6 }}>Route</div>
              <div className="row" style={{ gap: 16, alignItems: 'center', flexWrap: 'wrap' }}>
                <label className="row" style={{ gap: 4 }}>
                  <input type="radio" name="route_mode"
                    checked={draft.route_mode === 'auto'}
                    onChange={() => setDraft({ ...draft, route_mode: 'auto' })} />{' '}
                  Auto-detect
                </label>
                <label className="row" style={{ gap: 4 }}>
                  <input type="radio" name="route_mode"
                    checked={draft.route_mode === 'code'}
                    onChange={() => setDraft({ ...draft, route_mode: 'code' })} />{' '}
                  Code task
                </label>
                <label className="row" style={{ gap: 4 }}>
                  <input type="radio" name="route_mode"
                    checked={draft.route_mode === 'workflow'}
                    onChange={() => setDraft({ ...draft, route_mode: 'workflow' })} />{' '}
                  Workflow
                </label>
                {draft.route_mode === 'workflow' && (
                  <select
                    value={draft.route_workflow}
                    onChange={e => setDraft({ ...draft, route_workflow: e.target.value })}
                  >
                    <option value="">— pick a workflow —</option>
                    {workflows.map(w => (
                      <option key={w.id} value={w.id}>{w.label}</option>
                    ))}
                  </select>
                )}
              </div>

              {draft.route_mode === 'auto' && preview && (
                <div className="card"
                  style={{ marginTop: 8, padding: 8, fontSize: 12, opacity: 0.9 }}>
                  <div className="row" style={{ gap: 8, alignItems: 'baseline' }}>
                    <strong>Detected:</strong>
                    {preview.chosen.kind === 'workflow' ? (
                      <span>
                        ⚙ <code>{preview.chosen.workflow_id}</code>
                        <span style={{ marginLeft: 6, opacity: 0.7 }}>
                          (conf {preview.chosen.confidence.toFixed(2)})
                        </span>
                      </span>
                    ) : (
                      <span>📝 code task</span>
                    )}
                  </div>
                  {preview.chosen.rationale && (
                    <div style={{ opacity: 0.7, marginTop: 4 }}>
                      {preview.chosen.rationale}
                    </div>
                  )}
                  {(preview.candidates?.length ?? 0) > 1 && (
                    <details style={{ marginTop: 4 }}>
                      <summary>{preview.candidates!.length} candidates</summary>
                      <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
                        {preview.candidates!.map(c => (
                          <li key={c.workflow_id}>
                            <code>{c.workflow_id}</code> — {c.score.toFixed(2)}{' '}
                            {c.above_threshold ? '✓' : '⚠'} (
                            {c.reasons.join(', ')})
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              )}

              {draft.route_mode === 'workflow' && draft.route_workflow && (() => {
                const spec = workflows.find(w => w.id === draft.route_workflow);
                if (!spec) return null;
                return (
                  <div className="card"
                    style={{ marginTop: 8, padding: 8, fontSize: 12, opacity: 0.9 }}>
                    <div><strong>{spec.label}</strong></div>
                    <div style={{ opacity: 0.7, marginTop: 4 }}>{spec.description}</div>
                    {(spec.required_attachments?.length ?? 0) > 0 && (
                      <div style={{ marginTop: 4 }}>
                        Required attachments:{' '}
                        {spec.required_attachments!.map(a =>
                          <code key={a} style={{ marginRight: 4 }}>{a}</code>)}
                      </div>
                    )}
                  </div>
                );
              })()}
            </div>

            <div className="row" style={{ justifyContent: 'flex-end' }}>
              <button type="button" className="ghost" onClick={() => setCreating(false)}>Cancel</button>
              <button type="button" onClick={submit} disabled={!draft.title.trim()}>
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
          Role{' '}
          <select value={role} onChange={e => setRole(e.target.value)}>
            {ROLES.map(r => <option key={r} value={r}>{r || 'any'}</option>)}
          </select>
        </label>
        <label className="field">
          Status{' '}
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
