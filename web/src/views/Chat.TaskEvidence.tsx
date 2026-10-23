import { useState } from 'react';
import type { EvidenceStep, TaskEvidence } from './Chat.model';

const KIND_ICON: Record<string, string> = { command: '$', edit: '✎', read: '👁', tool: '⚙' };

function outcome(s: EvidenceStep): { text: string; color: string } {
  if (s.running) return { text: 'running', color: 'var(--info, #6aa6ff)' };
  if (s.code !== undefined && s.code !== null) {
    return { text: `exit ${s.code}`, color: s.code === 0 ? 'var(--ok, #3fb950)' : 'var(--err, #e5534b)' };
  }
  if (s.ok === true) return { text: 'ok', color: 'var(--ok, #3fb950)' };
  if (s.ok === false) return { text: 'failed', color: 'var(--err, #e5534b)' };
  return { text: '', color: '#8892a0' };
}

function Step({ s }: { s: EvidenceStep }) {
  const [open, setOpen] = useState(false);
  const o = outcome(s);
  const tests = [s.passed !== undefined ? `${s.passed} passed` : '', s.failed ? `${s.failed} failed` : '']
    .filter(Boolean).join(', ');
  return (
    <div style={{ fontSize: 11, padding: '1px 0' }}>
      <div style={{ display: 'flex', gap: 6, alignItems: 'baseline', cursor: s.output ? 'pointer' : 'default' }}
           onClick={() => s.output && setOpen(v => !v)}
           title={s.output ? (open ? 'Hide the output' : 'Show the output') : undefined}>
        <span style={{ width: 14, textAlign: 'center', color: '#8892a0', flexShrink: 0 }}>{KIND_ICON[s.kind] || '·'}</span>
        <span style={{ color: '#8892a0', flexShrink: 0 }}>{s.tool}</span>
        <code style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0,
                       background: 'none', border: 0, padding: 0 }}>{s.what}</code>
        {o.text && <span style={{ color: o.color, flexShrink: 0, fontWeight: 600 }}>{o.text}</span>}
        {tests && <span style={{ color: '#8892a0', flexShrink: 0 }}>{tests}</span>}
        {s.output && <span style={{ color: '#8892a0', flexShrink: 0 }}>{open ? '▾' : '▸'}</span>}
      </div>
      {open && s.output && (
        <pre style={{ margin: '2px 0 4px 20px', padding: '4px 6px', fontSize: 11, maxHeight: 220, overflow: 'auto',
                      whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{s.output}</pre>
      )}
    </div>
  );
}

/** The evidence behind a finished task: what it had to meet, and what the run
 *  actually did while it was open — recorded by the harness, not claimed. */
export function TaskEvidenceView({ ev }: { ev: TaskEvidence }) {
  return (
    <div className="task-evidence" style={{ margin: '2px 0 6px 20px', padding: '4px 8px',
                                            borderLeft: '2px solid var(--border-1)', fontSize: 11 }}>
      {ev.check && (
        <div style={{ marginBottom: 2 }}><span style={{ color: '#8892a0' }}>check:</span> {ev.check}</div>
      )}
      {ev.verdict && (
        <div style={{ marginBottom: 2, color: /^(FAILED|UNVERIFIED)|no file changes or checks/i.test(ev.verdict)
                        ? 'var(--warn, #d4a72c)' : undefined }}>
          <span style={{ color: '#8892a0' }}>verifier:</span> {ev.verdict}
        </div>
      )}
      {ev.shared_with && (
        <div style={{ color: '#8892a0', marginBottom: 2 }}>closed together with {ev.shared_with} — same work:</div>
      )}
      {ev.steps.map((s, i) => <Step key={i} s={s} />)}
      {ev.shown_of && (
        <div style={{ color: '#8892a0' }}>showing {ev.shown_of[0]} of {ev.shown_of[1]} actions (every failure is shown)</div>
      )}
      {ev.files && ev.files.length > 0 && (
        <div style={{ color: '#8892a0', marginTop: 2 }}>files changed: {ev.files.join(', ')}</div>
      )}
      {ev.note && (
        <div style={{ color: 'var(--warn, #d4a72c)' }}>⚠ {ev.note}</div>
      )}
    </div>
  );
}

/** One line for a closed task's evidence. */
export function evidenceSummary(ev?: TaskEvidence): string {
  if (!ev) return '';
  return ev.summary || ev.note || '';
}

const STATUS_COLOR: Record<string, string> = { done: '#3fb950', failed: '#e5534b' };

/** Under the answer: every task this turn closed, each with its proof in one
 *  line (expand for the commands and their output). Shown without asking. */
export function EvidenceCard({ items }: {
  items: ReadonlyArray<{ slug: string; title?: string; goal?: string; status: string; evidence?: TaskEvidence }>;
}) {
  const closed = items.filter(s => s.evidence && (s.status === 'done' || s.status === 'failed'));
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (closed.length === 0) return null;
  const weak = closed.filter(s => s.evidence!.note || s.status === 'failed').length;
  return (
    <div className="evidence-card" style={{ marginTop: 8, padding: '6px 10px', border: '1px solid var(--border-1)',
                                            borderRadius: 6, background: 'var(--bg-1)' }}>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
        Evidence <span className="muted xs" style={{ fontWeight: 400 }}>
          — what was run and changed for each finished task, recorded by AIForge
          {weak ? ` · ${weak} need${weak === 1 ? 's' : ''} a look` : ''}
        </span>
      </div>
      {closed.map(s => {
        const ev = s.evidence!;
        const label = String(s.title || s.goal || s.slug).replace(/\s[—–-]{1,2}\s*check:.*$/i, '');
        const isOpen = !!open[s.slug];
        return (
          <div key={s.slug} style={{ fontSize: 12 }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', cursor: 'pointer', padding: '1px 0' }}
                 onClick={() => setOpen(m => ({ ...m, [s.slug]: !m[s.slug] }))}
                 title={isOpen ? 'Hide the detail' : 'Show the commands, output and checks'}>
              <span style={{ color: STATUS_COLOR[s.status] || '#8892a0', fontWeight: 600, flexShrink: 0 }}>
                {s.status === 'done' ? '✓' : '✗'}
              </span>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>{label}</span>
              <span style={{ marginLeft: 'auto', flexShrink: 0, color: ev.note ? 'var(--warn, #d4a72c)' : '#8892a0' }}>
                {evidenceSummary(ev)} {isOpen ? '▾' : '▸'}
              </span>
            </div>
            {isOpen && <TaskEvidenceView ev={ev} />}
          </div>
        );
      })}
    </div>
  );
}

/** The items an answer's Evidence card lists: the tasks that closed with
 *  evidence, then the answer's own record (work no task shows). */
export function evidenceItemsOf(subtasks: ReadonlyArray<SubtaskLike> | undefined,
                                turn: TaskEvidence | undefined): SubtaskLike[] {
  const items: SubtaskLike[] = [...(subtasks || []).filter(
    s => s.evidence && (s.status === 'done' || s.status === 'failed'))];
  if (turn) {
    items.push({ slug: '__answer', status: 'done', evidence: turn,
                 title: items.length ? 'Other work in this answer' : 'This answer' });
  }
  return items;
}
type SubtaskLike = { slug: string; title?: string; goal?: string; status: string; evidence?: TaskEvidence };
