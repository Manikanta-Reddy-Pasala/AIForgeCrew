import { SUBTASK_COLORS } from './TicketDetail.helpers';

export function SubtaskProgress(
  { subtasks, progress, onRunParallel }:
  Readonly<{ subtasks: any[]; progress?: { total: number; done: number; fraction: number; counts: Record<string, number> }; onRunParallel?: () => void }>,
) {
  const total = progress?.total ?? subtasks.length;
  const done = progress?.done ?? 0;
  const counts = progress?.counts ?? {};
  const anyRunning = (counts['running'] || 0) > 0;
  // ordered segments for the stacked bar
  const order = ['done', 'skipped', 'running', 'failed', 'pending'];
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <h2 style={{ fontSize: 14, margin: 0 }}>
          Subtasks <span className="muted">({done}/{total} done)</span>
        </h2>
        <div className="row" style={{ gap: 10, fontSize: 'var(--fs-xs)', alignItems: 'center' }}>
          {order.filter(k => counts[k]).map(k => (
            <span key={k} style={{ color: SUBTASK_COLORS[k] }}>
              ● {k} {counts[k]}
            </span>
          ))}
          {onRunParallel && done < total && (
            <button type="button" className="ghost sm" onClick={onRunParallel} disabled={anyRunning}
                    title="Run the subtasks concurrently, each in its own worktree, then merge">
              {anyRunning ? 'Running…' : '⇉ Run in parallel'}
            </button>
          )}
        </div>
      </div>
      {/* stacked progress bar */}
      <div style={{ display: 'flex', height: 10, borderRadius: 5, overflow: 'hidden', background: 'var(--bg-2,#222)' }}>
        {order.map(k => counts[k] ? (
          <div key={k} title={`${k}: ${counts[k]}`}
               style={{ width: `${(counts[k] / total) * 100}%`, background: SUBTASK_COLORS[k] }} />
        ) : null)}
      </div>
      {/* review verdict — each done subtask is build/test-validated */}
      {(() => {
        const failed = counts['failed'] || 0;
        const remaining = total - done - failed;
        let verdict: string;
        if (done === total) verdict = 'All subtasks done & validated ✓';
        else if (failed > 0) verdict = 'Some subtasks failed — needs attention';
        else if (remaining > 0) verdict = 'In progress';
        else verdict = 'Review';
        let tone: string;
        if (done === total) tone = '#3fb950';
        else if (failed > 0) tone = '#e5534b';
        else tone = '#6aa6ff';
        return (
          <div className="row" style={{ marginTop: 8, justifyContent: 'space-between',
               alignItems: 'center', fontSize: 12 }}>
            <span style={{ color: tone, fontWeight: 600 }}>{verdict}</span>
            <span className="muted">
              ✓ {done} done &amp; validated · ✗ {failed} failed · ◷ {remaining} remaining
            </span>
          </div>
        );
      })()}
      {/* per-subtask list */}
      <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 4 }}>
        {subtasks.map((s, i) => (
          <div key={s.slug || i} className="row" style={{ gap: 8, alignItems: 'center', fontSize: 13 }}>
            <span style={{
              flexShrink: 0, width: 64, textAlign: 'center', fontSize: 11, fontWeight: 600,
              color: SUBTASK_COLORS[s.status] || SUBTASK_COLORS.pending,
              border: `1px solid ${SUBTASK_COLORS[s.status] || SUBTASK_COLORS.pending}`,
              borderRadius: 4, padding: '1px 4px',
            }}>{s.status}</span>
            <span className="mono muted" style={{ flexShrink: 0, fontSize: 11 }}>{s.slug}</span>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.goal || s.title || s.slug}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
