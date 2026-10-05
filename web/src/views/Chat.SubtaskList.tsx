import { useState } from 'react';
import { SubtaskItem } from './Chat.types';
import { clickable } from '../a11y';

const SUBTASK_COLORS: Record<string, string> = {
  done: '#3fb950', skipped: '#5a6472', running: '#6aa6ff',
  failed: '#e5534b', pending: '#8892a0', planned: '#a371f7',
  won: '#d4a72c', cancelled: '#5a6472', left: '#b08800',
};

// What a status reads as on its chip. "left" = the turn ended before the run
// closed the task.
const STATUS_LABEL: Record<string, string> = { left: 'not done' };
const SETTLED = ['done', 'won', 'skipped', 'failed', 'cancelled', 'left', 'planned', 'graded'];

export function SubtaskList({ items, onViewSpec, onDismiss }: Readonly<{
  items: SubtaskItem[]; onViewSpec?: () => void;
  /** hide a list whose work is over (offered only when nothing is going on) */
  onDismiss?: () => void;
}>) {
  // Default COLLAPSED — the header line is the crisp, live at-a-glance view;
  // click to expand the full list.
  const [open, setOpen] = useState(false);
  const counts = items.reduce((m, s) => { m[s.status] = (m[s.status] || 0) + 1; return m; }, {} as Record<string, number>);
  const done = (counts['done'] || 0) + (counts['won'] || 0) + (counts['skipped'] || 0);
  const order = ['done', 'won', 'running', 'failed', 'left', 'cancelled', 'planned', 'pending', 'skipped'];
  // Live "current" subtask shown in the collapsed header so status reads in
  // real time without expanding.
  const current = items.find(s => s.status === 'running')
    || items.find(s => !SETTLED.includes(s.status));
  const allSettled = items.every(s => SETTLED.includes(s.status));
  const pct = items.length ? Math.round((done / items.length) * 100) : 0;
  return (
    <div style={{ border: '1px solid var(--border-1)', borderRadius: 6, padding: '6px 10px', margin: '4px 0', background: 'var(--bg-1,#0d1117)' }}>
      <div {...clickable(() => setOpen(v => !v))}
        style={{ fontSize: 12, fontWeight: 600, marginBottom: open ? 6 : 0, cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10 }}>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {open ? '▾' : '▸'} Tasks <span style={{ color: '#8892a0' }}>{done}/{items.length}</span>
          {!open && current && (
            <span style={{ color: SUBTASK_COLORS[current.status] || SUBTASK_COLORS.running, fontWeight: 500, marginLeft: 8 }}>
              ▸ {current.slug}
            </span>
          )}
        </span>
        <span style={{ display: 'flex', gap: 6, fontSize: 10, fontWeight: 500, flexShrink: 0, alignItems: 'center' }}>
          {order.filter(k => counts[k]).map(k => (
            <span key={k} style={{ color: SUBTASK_COLORS[k] }}>● {counts[k]}</span>
          ))}
          <span style={{ color: '#8892a0' }}>{pct}%</span>
          {onViewSpec && (
            <button type="button" className="ghost xs" title="Preview SPEC.md (the plan's requirements)"
                    onClick={(e) => { e.stopPropagation(); onViewSpec(); }}
                    style={{ cursor: 'pointer', padding: '1px 6px', fontSize: 10, whiteSpace: 'nowrap' }}>
              📄 SPEC.md
            </button>
          )}
          {onDismiss && allSettled && (
            <button type="button" className="ghost xs" title="Hide this task list — its work is over"
                    aria-label="Hide the task list"
                    onClick={(e) => { e.stopPropagation(); onDismiss(); }}
                    onKeyDown={(e) => e.stopPropagation()}
                    style={{ cursor: 'pointer', padding: '1px 6px', fontSize: 10 }}>
              ✕
            </button>
          )}
        </span>
      </div>
      {/* progress bar */}
      <div style={{ display: open ? 'flex' : 'none', height: 5, borderRadius: 3, overflow: 'hidden', background: 'var(--bg-2,#222)', marginBottom: 8 }}>
        {order.map(k => counts[k] ? <div key={k} style={{ width: `${(counts[k] / items.length) * 100}%`, background: SUBTASK_COLORS[k] }} /> : null)}
      </div>
      {open && items.map((s, i) => {
        // Producers disagree on the field name (goal vs title), and a subtask
        // may legitimately have neither — the slug is always there. Reading
        // one key blind is what took the whole chat view down when the panel
        // was expanded.
        const label = String(s.goal || s.title || s.slug || '');
        return (
        <div key={s.slug || i} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, padding: '2px 0' }}>
          <span style={{ flexShrink: 0, width: 58, textAlign: 'center', fontSize: 10, fontWeight: 600,
            color: SUBTASK_COLORS[s.status] || SUBTASK_COLORS.pending,
            border: `1px solid ${SUBTASK_COLORS[s.status] || SUBTASK_COLORS.pending}`, borderRadius: 4, padding: '1px 3px' }}>{STATUS_LABEL[s.status] || s.status}</span>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
            <span style={{ color: '#8892a0', fontFamily: 'monospace', marginRight: 6 }}>{s.slug}</span>
            <span style={{ color: '#8892a0' }}>{label.length > 60 ? label.slice(0, 60) + '…' : label}</span>
          </span>
        </div>
        );
      })}
    </div>
  );
}
