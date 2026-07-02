import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api, type Job, type JobDraft, type JobPreview } from '../api';
import { Icon } from '../icons';

// Launch a chat-driven "builder" conversation. Each opens a fresh Chat session
// in the given builder mode (the agent interviews you, then finalizes).
// Jobs screen only builds JOBS. Skill / workflow / rule builders live on the
// Library page (their respective screens).
const CHAT_BUILDERS: { kind: string; label: string; icon: keyof typeof Icon; title: string }[] = [
  { kind: 'job', label: 'New job via chat', icon: 'Refresh', title: 'Chat with an agent to build & schedule a recurring job' },
];

export default function Jobs() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { data: jobs, isLoading } = useQuery({
    queryKey: ['jobs'], queryFn: api.listJobs, refetchInterval: 30_000,
  });
  const [creating, setCreating] = useState(false);
  const [instructions, setInstructions] = useState('');
  const [preview, setPreview] = useState<JobPreview | null>(null);
  const [draft, setDraft] = useState<JobDraft | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = () => qc.invalidateQueries({ queryKey: ['jobs'] });

  const doPreview = async () => {
    setBusy(true);
    try {
      const p = await api.previewJob(instructions);
      setPreview(p);
      setDraft(p.ok && p.draft ? { ...p.draft } : null);
    } catch (e: any) {
      toast.error(e?.message || 'preview failed');
    } finally { setBusy(false); }
  };

  const doConfirm = async () => {
    if (!draft) return;
    setBusy(true);
    try {
      await api.createJob(draft);
      toast.success('Job scheduled');
      setCreating(false); setInstructions(''); setPreview(null); setDraft(null);
      refresh();
    } catch (e: any) {
      toast.error(e?.message || 'save failed');
    } finally { setBusy(false); }
  };

  const doRunNow = async (id: number) => {
    try {
      const r = await api.runJobNow(id);
      if (r.ok) toast.success('Fired — ticket created');
      else toast.error('Fire failed — see job status');
      refresh();
    } catch (e: any) { toast.error(e?.message || 'run failed'); }
  };

  const doToggle = async (jb: Job) => {
    try { await api.patchJob(jb.id, { enabled: !jb.enabled }); refresh(); }
    catch (e: any) { toast.error(e?.message || 'update failed'); }
  };

  const doDelete = async (id: number) => {
    if (!window.confirm('Delete this job? Its past tickets are kept.')) return;
    try { await api.deleteJob(id); refresh(); }
    catch (e: any) { toast.error(e?.message || 'delete failed'); }
  };

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Scheduled Jobs</h1>
          <div className="subtitle">
            {(jobs?.length ?? 0).toLocaleString()} job{jobs?.length === 1 ? '' : 's'}
          </div>
        </div>
        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          {CHAT_BUILDERS.map(b => {
            const I = Icon[b.icon];
            return (
              <button
                key={b.kind}
                className="ghost"
                title={b.title}
                onClick={() => navigate(`/chat?builder=${b.kind}`)}
              >
                <I size={14} /> {b.label}
              </button>
            );
          })}
          <button onClick={() => setCreating(c => !c)}>
            {creating ? <><Icon.X size={14} /> Cancel</> : <><Icon.Plus size={14} /> New job</>}
          </button>
        </div>
      </div>

      {creating && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-header"><h2>New job</h2></div>
          <div className="stack">
            <label className="field">
              Describe the job in plain words
              <textarea
                rows={3}
                autoFocus
                value={instructions}
                placeholder="e.g. pull all the GitLab comments every day at 8am"
                onChange={e => setInstructions(e.target.value)}
              />
            </label>
            <div className="row">
              <button disabled={busy || !instructions.trim()} onClick={doPreview}>
                <Icon.Sparkles size={14} /> {busy ? 'Parsing…' : 'Preview'}
              </button>
            </div>
            {preview && !preview.ok && (
              <div style={{ color: 'var(--err)', fontSize: 13 }}>
                {preview.error}
              </div>
            )}
            {preview?.ok && draft && (
              <div className="card" style={{ padding: 12 }}>
                <p style={{ marginTop: 0 }}>
                  <b>{preview.human_schedule}</b> — next runs:{' '}
                  {(preview.next_runs || []).map(t =>
                    new Date(t).toLocaleString()).join(' · ')}
                </p>
                <div className="stack">
                  <label className="field">
                    Name
                    <input
                      value={draft.name}
                      onChange={e => setDraft({ ...draft, name: e.target.value })}
                    />
                  </label>
                  <label className="field">
                    Ticket title
                    <input
                      value={draft.ticket_title}
                      onChange={e => setDraft({ ...draft, ticket_title: e.target.value })}
                    />
                  </label>
                  <label className="field">
                    Ticket body (what the agent will do)
                    <textarea
                      rows={4}
                      value={draft.ticket_body}
                      onChange={e => setDraft({ ...draft, ticket_body: e.target.value })}
                    />
                  </label>
                  <label className="field">
                    Cron
                    <input
                      value={draft.cron}
                      onChange={e => setDraft({ ...draft, cron: e.target.value })}
                    />
                  </label>
                  <div className="row" style={{ justifyContent: 'flex-end' }}>
                    <button className="ghost" onClick={() => { setPreview(null); setDraft(null); }}>
                      Discard
                    </button>
                    <button disabled={busy} onClick={doConfirm}>
                      <Icon.Check size={14} /> Confirm &amp; schedule
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        {isLoading ? (
          <div className="empty"><div className="skeleton" style={{ width: 200, height: 16 }} /></div>
        ) : !jobs?.length ? (
          <div className="empty">
            <div className="empty-icon"><Icon.Refresh size={18} /></div>
            <div style={{ color: 'var(--fg-0)', fontWeight: 500 }}>No scheduled jobs yet</div>
            <div>Create one above — describe it in plain words.</div>
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Schedule</th>
                <th>Next run</th>
                <th>Last run</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {jobs.map(jb => (
                <tr key={jb.id}>
                  <td style={{ color: 'var(--fg-0)' }}>{jb.name}</td>
                  <td className="small muted">{jb.human_schedule}</td>
                  <td className="small muted nowrap">{new Date(jb.next_run_at).toLocaleString()}</td>
                  <td className="small muted nowrap">
                    {jb.last_run_at ? new Date(jb.last_run_at).toLocaleString() : '—'}
                  </td>
                  <td>
                    {jb.last_error
                      ? <span className="chip err" title={jb.last_error}>error</span>
                      : <span className={`chip ${jb.enabled ? 'ok' : ''}`}>
                          {jb.enabled ? 'active' : 'paused'}
                        </span>}
                  </td>
                  <td className="nowrap">
                    <button className="ghost" onClick={() => doRunNow(jb.id)}>
                      Run now
                    </button>
                    <button className="ghost" onClick={() => doToggle(jb)}>
                      {jb.enabled ? 'Pause' : 'Resume'}
                    </button>
                    <button className="ghost" onClick={() => doDelete(jb.id)}>
                      <Icon.Trash size={14} /> Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
