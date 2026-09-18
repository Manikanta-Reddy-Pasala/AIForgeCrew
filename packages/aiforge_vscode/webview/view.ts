// HTML for the chat log: past turns, the live turn, approval and question
// cards. Pure string building (every piece of model text goes through
// escapeHtml / renderMarkdown); buttons carry data-action attributes that
// main.ts handles with one delegated listener.
import type { LiveTurn } from '../../../web/src/views/Chat.model';
import { isInternalPath, writtenPath } from '../src/fileTools';
import { renderDiff } from './diff';
import { escapeHtml, renderMarkdown } from './md';

export type Msg = { id: number; role: 'user' | 'assistant'; content: string; steps: any[];
                    checkpoint_sha?: string | null };

export type FileChange = { path: string; status: string; additions?: number;
                           deletions?: number; diff?: string };

/** Every file a turn changed: the `changes` step when there is one (it has
 *  diffs and counts), plus files written by tool calls it does not list. */
export function turnChanges(steps: any[], cwd = ''): FileChange[] {
  // The agent names a file absolutely in a tool call and relative to the repo
  // in the summary: one file, one row.
  const rel = (p: string) => relativeTo(p, cwd);
  const out = new Map<string, FileChange>();
  for (const s of steps) {
    const kind = s?.kind ?? s?.type;
    if (kind === 'changes') {
      for (const f of s.files ?? []) {
        if (typeof f?.path === 'string' && !isInternalPath(f.path)) {
          out.set(rel(f.path), { path: rel(f.path), status: f.status || 'changed',
                                 additions: f.additions, deletions: f.deletions,
                                 diff: f.diff || undefined });
        }
      }
    } else if (kind === 'tool') {
      const w = writtenPath(s);
      const p = w ? rel(w) : null;
      if (p && !isInternalPath(p) && !out.has(p)) out.set(p, { path: p, status: 'written' });
    }
  }
  return [...out.values()];
}

export function relativeTo(p: string, cwd: string): string {
  const base = cwd.replace(/\/+$/, '');
  return base && p.startsWith(base + '/') ? p.slice(base.length + 1) : p;
}

// A draft being written that is really a tool call (THOUGHT/ACTION/ARGS_JSON
// text) is noise; the call gets its own row the moment it runs.
function visibleDraft(d: string | undefined): string {
  if (!d || /\b(ACTION|ARGS_JSON)\s*:/.test(d)) return '';
  return d.replace(/^\s*THOUGHT\s*:\s*/i, '');
}

const attr = (s: string) => escapeHtml(s);

function button(action: string, label: string, data: Record<string, string> = {}, title = ''): string {
  const extra = Object.entries(data).map(([k, v]) => ` data-${k}="${attr(v)}"`).join('');
  return `<button class="link" data-action="${action}"${extra}${title ? ` title="${attr(title)}"` : ''}>${label}</button>`;
}

function changesCard(files: FileChange[], sha: string | null, live: boolean): string {
  if (!files.length) return '';
  const adds = files.reduce((n, f) => n + (f.additions ?? 0), 0);
  const dels = files.reduce((n, f) => n + (f.deletions ?? 0), 0);
  const counted = files.some(f => f.additions !== undefined);
  const rows = files.map(f => {
    const shaData: Record<string, string> = sha ? { sha } : {};
    const counts = f.additions !== undefined
      ? `<span class="add">+${f.additions}</span> <span class="del">−${f.deletions ?? 0}</span>` : '';
    const actions = [
      button('openFile', 'Diff', { path: f.path, ...shaData }, 'Open the before/after diff in the editor'),
      button('explain', 'Explain', { path: f.path }, 'What changed here and why, in simple English'),
      !live && sha ? button('undoFile', 'Undo', { path: f.path, sha },
                            'Put this file back the way it was before this turn') : '',
    ].join('');
    const inline = f.diff ? `<details class="filediff"><summary>show changes</summary>${renderDiff(f.diff)}</details>` : '';
    return `<div class="file"><div class="filerow"><span class="st st-${attr(f.status)}">${attr(f.status)}</span>
      <span class="path" title="${attr(f.path)}">${escapeHtml(f.path)}</span>${counts}
      <span class="acts">${actions}</span></div>${inline}</div>`;
  }).join('');
  const total = counted ? ` <span class="add">+${adds}</span> <span class="del">−${dels}</span>` : '';
  return `<div class="changes"><div class="changes-head">Changed ${files.length} file${files.length === 1 ? '' : 's'}${total}
    ${button('explain', 'Explain all in simple English')}</div>${rows}</div>`;
}

function stepRow(s: any): string {
  const kind = s?.kind ?? s?.type;
  if (kind === 'thought') {
    const who = s.role && s.role !== 'system' ? `<span class="role">${escapeHtml(s.role)}</span>` : '';
    return `<div class="step thought${s.role === 'system' ? ' sys' : ''}">${who}${escapeHtml(String(s.text ?? ''))}</div>`;
  }
  if (kind === 'tool') {
    const r = s.result || {};
    const failed = r.ok === false || r.error || r.blocked;
    const mark = s.pending ? '…' : failed ? '✗' : '✓';
    const arg = s.args?.path ?? s.args?.cmd ?? s.args?.query ?? s.args?.pattern ?? '';
    return `<div class="step tool${failed ? ' bad' : ''}"><span class="mark">${mark}</span>
      <b>${escapeHtml(String(s.name ?? ''))}</b> <span class="arg">${escapeHtml(String(arg)).slice(0, 160)}</span></div>`;
  }
  if (kind === 'error') return `<div class="step err">${escapeHtml(String(s.text ?? ''))}</div>`;
  if (kind === 'message') {
    return `<div class="step msg"><span class="role">${escapeHtml(s.role || 'agent')}</span>${renderMarkdown(String(s.text ?? ''))}</div>`;
  }
  return '';
}

function stepsBlock(steps: any[], open: boolean): string {
  const rows = steps.filter(s => (s?.kind ?? s?.type) !== 'changes').map(stepRow).filter(Boolean);
  if (!rows.length) return '';
  return `<details class="steps"${open ? ' open' : ''}><summary>${rows.length} step${rows.length === 1 ? '' : 's'}</summary>${rows.join('')}</details>`;
}

export function renderHistory(msgs: Msg[], cwd = ''): string {
  let lastSha: string | null = null;
  return msgs.map(m => {
    if (m.role === 'user') {
      lastSha = m.checkpoint_sha ?? null;
      const back = lastSha ? button('restore', '↺ Go back to before this', { sha: lastSha },
        'Put the folder back the way it was before this message') : '';
      return `<div class="turn user"><div class="bubble">${escapeHtml(m.content)}</div>${back}</div>`;
    }
    return `<div class="turn agent">${stepsBlock(m.steps || [], false)}
      <div class="answer">${renderMarkdown(m.content || '')}</div>
      ${changesCard(turnChanges(m.steps || [], cwd), lastSha, false)}</div>`;
  }).join('');
}

export function renderLive(t: LiveTurn | null, pendingUser: string | null, cwd = ''): string {
  const user = pendingUser ? `<div class="turn user"><div class="bubble">${escapeHtml(pendingUser)}</div></div>` : '';
  if (!t) return user;
  const text = t.text || t.streamText || '';
  const shown = t.streaming && !text ? visibleDraft(t.draft) : '';
  const draft = shown ? `<div class="draft">${escapeHtml(shown)}</div>` : '';
  const working = t.streaming ? '<div class="working">working…</div>' : '';
  const ask = t.awaiting ? '<div class="ask">The agent is waiting for your answer — reply below.</div>' : '';
  return `${user}<div class="turn agent live">${stepsBlock(t.steps, t.streaming)}${draft}
    <div class="answer">${renderMarkdown(text)}</div>${ask}
    ${changesCard(turnChanges(t.steps, cwd), null, true)}${working}</div>`;
}

export function renderApproval(ev: any | null): string {
  if (!ev) return '';
  const preview = ev.preview ? `<div class="preview">${renderMarkdown(String(ev.preview))}</div>` : '';
  return `<div class="approval"><div><b>Approve ${escapeHtml(String(ev.name ?? 'this action'))}?</b></div>
    ${ev.reason ? `<div class="muted">${escapeHtml(String(ev.reason))}</div>` : ''}${preview}
    <div class="row">${button('approve', 'Approve', { id: String(ev.id) })}
    ${button('reject', 'Reject', { id: String(ev.id) })}</div></div>`;
}
