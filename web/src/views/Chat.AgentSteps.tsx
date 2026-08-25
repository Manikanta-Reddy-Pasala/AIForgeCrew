import { useState } from 'react';
import { Icon } from '../icons';
import { AgentStep, ChangeFile } from './Chat.types';

// ── Agent step row ─────────────────────────────────────────────────────────────

// Small pill showing WHICH agent produced a step (team mode). Stable
// color per role name so the eye can track each agent across steps.
function AgentBadge({ role }: Readonly<{ role?: string }>) {
  if (!role) return null;
  let h = 0;
  for (let i = 0; i < role.length; i++) h = (h * 31 + role.charCodeAt(i)) % 360;
  return (
    <span style={{
      flexShrink: 0,
      fontFamily: 'var(--font-mono)',
      fontStyle: 'normal',
      fontSize: '10px',
      fontWeight: 600,
      padding: '1px 6px',
      borderRadius: 999,
      color: `hsl(${h} 70% 30%)`,
      background: `hsl(${h} 70% 92%)`,
      border: `1px solid hsl(${h} 60% 80%)`,
      marginTop: 1,
      whiteSpace: 'nowrap',
    }} title={`agent: ${role}`}>{role}</span>
  );
}

// A thought/reasoning step. Long chain-of-thought (reasoning models dump
// their whole "Thinking Process…") is collapsed to one line so each
// agent reads as a clean structured step; click to expand the full text.
function ThoughtRow({ step }: Readonly<{ step: Extract<AgentStep, { kind: 'thought' }> }>) {
  // The PERSISTED path defaults this (Chat.helpers.toAgentStep), the live SSE
  // path does not — it copies evt.text straight off an `any`. That asymmetry
  // is exactly the state the subtask label was in before it took the chat view
  // down, and it throws the same sentence.
  const text = String(step.text ?? '');
  const long = text.length > 180;
  const [open, setOpen] = useState(!long);
  const preview = long && !open
    ? text.replace(/\s+/g, ' ').slice(0, 140) + '…'
    : text;
  return (
    <div style={{
      display: 'flex', gap: 6, alignItems: 'flex-start',
      padding: '5px 10px',
      background: 'var(--bg-1)',
      border: '1px solid var(--border-0)',
      borderRadius: 'var(--r-sm)',
      fontStyle: 'italic',
      color: 'var(--fg-2)',
      fontSize: 'var(--fs-xs)',
      lineHeight: 1.5,
    }}>
      <span style={{ flexShrink: 0, marginTop: 1 }}>💭</span>
      <AgentBadge role={step.role} />
      <span style={{ whiteSpace: 'pre-wrap', flex: 1 }}>{preview}</span>
      {long && (
        <button type="button"
          onClick={() => setOpen(o => !o)}
          className="ghost sm"
          style={{ flexShrink: 0, fontSize: 10, fontStyle: 'normal', padding: '0 6px' }}
        >
          {open ? 'collapse' : 'expand'}
        </button>
      )}
    </div>
  );
}

function DiffBody({ diff }: Readonly<{ diff: string }>) {
  return (
    <pre style={{
      margin: 0, padding: '8px 10px', overflowX: 'auto',
      fontSize: 'var(--fs-xs)', lineHeight: 1.55, fontFamily: 'var(--font-mono)',
      background: 'var(--bg-code)', borderTop: '1px solid var(--border-0)',
    }}>
      {/* key=index: immutable diff text rendered once; lines legitimately
          duplicate (blank/context lines) so a content key would collide, and
          the list never reorders. (S6479 exception) */}
      {diff.split('\n').map((ln, i) => {
        let color: string | undefined;
        let bg: string | undefined;
        if (/^\+\+\+|^---|^diff |^index |^@@/.test(ln)) { color = 'var(--fg-3)'; }
        else if (ln.startsWith('+')) { color = 'var(--ok)'; bg = 'rgba(63,185,80,.10)'; }
        else if (ln.startsWith('-')) { color = 'var(--err)'; bg = 'rgba(248,81,73,.10)'; }
        return <div key={i} style={{ color, background: bg, whiteSpace: 'pre' }}>{ln || ' '}</div>;
      })}
    </pre>
  );
}

function ChangeFileRow({ file }: Readonly<{ file: ChangeFile }>) {
  const [open, setOpen] = useState(false);
  const STATUS_COLOR: Record<string, string> = { added: 'var(--ok)', deleted: 'var(--err)' };
  const statusColor = STATUS_COLOR[file.status] ?? 'var(--accent)';
  return (
    <div style={{ borderTop: '1px solid var(--border-0)' }}>
      <button type="button" onClick={() => setOpen(o => !o)} style={{
        width: '100%', display: 'flex', alignItems: 'center', gap: 8,
        padding: '6px 10px', background: 'transparent', border: 0, cursor: 'pointer',
        fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-xs)', textAlign: 'left',
      }}>
        <span style={{ color: 'var(--fg-3)', transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .1s' }}>▸</span>
        <span style={{ color: statusColor, textTransform: 'uppercase', fontSize: 10, width: 62, flexShrink: 0 }}>{file.status}</span>
        <span style={{ color: 'var(--fg-1)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>{file.path}</span>
        {file.additions > 0 && <span style={{ color: 'var(--ok)' }}>+{file.additions}</span>}
        {file.deletions > 0 && <span style={{ color: 'var(--err)' }}>−{file.deletions}</span>}
      </button>
      {open && <DiffBody diff={file.diff} />}
    </div>
  );
}

function ChangesView({ files, summary }: Readonly<{ files: ChangeFile[]; summary: { files: number; additions: number; deletions: number } }>) {
  return (
    <div style={{ border: '1px solid var(--border-1)', borderRadius: 'var(--r-md)', overflow: 'hidden', background: 'var(--bg-1)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', background: 'var(--bg-2)', fontSize: 'var(--fs-xs)', fontWeight: 600 }}>
        <Icon.GitBranch size={14} />
        <span>Changes</span>
        <span style={{ color: 'var(--fg-3)', fontWeight: 400 }}>
          {summary.files} file{summary.files === 1 ? '' : 's'}
        </span>
        {summary.additions > 0 && <span style={{ color: 'var(--ok)' }}>+{summary.additions}</span>}
        {summary.deletions > 0 && <span style={{ color: 'var(--err)' }}>−{summary.deletions}</span>}
      </div>
      {files.map((f) => <ChangeFileRow key={f.path} file={f} />)}
    </div>
  );
}

export function AgentStepRow({ step }: Readonly<{ step: AgentStep }>) {
  if (step.kind === 'changes') {
    return <ChangesView files={step.files} summary={step.summary} />;
  }
  if (step.kind === 'thought') {
    return <ThoughtRow step={step} />;
  }
  if (step.kind === 'tool') {
    const res = step.result as any;
    const ok = res?.ok !== false && !res?.error;
    let snippet: string;
    if (step.pending) snippet = 'running…';
    else if (ok) snippet = res?.output ? String(res.output).slice(0, 120) : 'ok';
    else snippet = res?.error ? String(res.error).slice(0, 120) : 'error';
    const toolTextColor = (step.pending || ok) ? 'var(--fg-1)' : 'var(--err)';
    let arrowColor: string;
    if (step.pending) arrowColor = 'var(--fg-2, var(--fg-1))';
    else if (ok) arrowColor = 'var(--ok)';
    else arrowColor = 'var(--err)';
    return (
      <div style={{
        display: 'flex', gap: 6, alignItems: 'flex-start',
        padding: '5px 10px',
        background: 'var(--bg-1)',
        border: '1px solid var(--border-0)',
        borderRadius: 'var(--r-sm)',
        fontSize: 'var(--fs-xs)',
        lineHeight: 1.5,
        fontFamily: 'var(--font-mono)',
        color: toolTextColor,
      }}>
        <span style={{ flexShrink: 0, marginTop: 1 }}>{step.pending ? '⏳' : '🔧'}</span>
        <AgentBadge role={step.role} />
        <span>
          <strong>{step.name}</strong>
          {'('}
          {Object.entries(step.args as Record<string, unknown>).slice(0, 3).map(([k, v], i) =>
            `${i > 0 ? ', ' : ''}${k}=${JSON.stringify(v).slice(0, 40)}`
          ).join('')}
          {')'}
          {' → '}
          <span style={{ color: arrowColor }}>
            {snippet}
          </span>
        </span>
      </div>
    );
  }
  if (step.kind === 'error') {
    return (
      <div style={{
        display: 'flex', gap: 6, alignItems: 'flex-start',
        padding: '5px 10px',
        background: 'var(--err-soft)',
        border: '1px solid transparent',
        borderRadius: 'var(--r-sm)',
        fontSize: 'var(--fs-xs)',
        lineHeight: 1.5,
        color: 'var(--err)',
      }}>
        <span style={{ flexShrink: 0, marginTop: 1 }}>✗</span>
        <span>{step.text}</span>
      </div>
    );
  }
  return null;
}
