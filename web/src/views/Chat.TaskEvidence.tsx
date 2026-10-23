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
    <div className="task-evidence" style={{ margin: '2px 0 6px 66px', padding: '4px 8px',
                                            borderLeft: '2px solid var(--border-1)', fontSize: 11 }}>
      {ev.check && (
        <div style={{ marginBottom: 2 }}><span style={{ color: '#8892a0' }}>check:</span> {ev.check}</div>
      )}
      {ev.verdict && (
        <div style={{ marginBottom: 2 }}><span style={{ color: '#8892a0' }}>verified:</span> {ev.verdict}</div>
      )}
      {ev.shared_with && (
        <div style={{ color: '#8892a0', marginBottom: 2 }}>closed together with {ev.shared_with} — same work:</div>
      )}
      {ev.steps.map((s, i) => <Step key={i} s={s} />)}
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
