import { useEffect, useRef, useState } from 'react';
import { chatProcesses, chatProcessLog, chatProcessKill, ChatProcess } from '../api';

const LIST_POLL_MS = 2000;
const LOG_POLL_MS = 1000;
/** Characters kept per open log (the oldest are dropped first). */
const KEEP_CHARS = 200_000;
/** First read of a log: its last 64 KB per stream. */
const FIRST_READ = -64 * 1024;

type Line = { err: boolean; text: string };

function since(epochS: number, untilS?: number): string {
  const s = Math.max(0, Math.round((untilS ?? Date.now() / 1000) - epochS));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function stateOf(p: ChatProcess): { cls: string; text: string } {
  if (p.running) return { cls: 'chip info', text: 'running' };
  if (p.stopped) return { cls: 'chip warn', text: 'stopped' };
  return p.code === 0 ? { cls: 'chip ok', text: 'exit 0' }
                      : { cls: 'chip err', text: `exit ${p.code ?? '?'}` };
}

function title(p: ChatProcess): string {
  return p.label || p.cmd;
}

/** One live log: tails the command by byte offset, newest at the bottom. */
function LogTile({ sessionId, proc, onClose }: {
  sessionId: number;
  proc: ChatProcess;
  onClose: () => void;
}) {
  const [lines, setLines] = useState<Line[]>([]);
  const [follow, setFollow] = useState(true);
  const [wide, setWide] = useState(false);
  const [gone, setGone] = useState(false);
  const offsets = useRef<number[]>([]);
  const body = useRef<HTMLPreElement>(null);
  const running = useRef(proc.running);
  running.current = proc.running;

  useEffect(() => {
    let live = true;
    let timer: number | undefined;
    let misses = 0;
    offsets.current = [];
    setLines([]);
    setGone(false);
    async function tick() {
      try {
        const offs = offsets.current.length ? offsets.current : [FIRST_READ, FIRST_READ];
        const r = await chatProcessLog(sessionId, proc.id, offs);
        if (!live) return;
        misses = 0;
        offsets.current = r.streams.map(s => s.next);
        const add: Line[] = [];
        r.streams.forEach((s, i) => {
          if (s.skipped) add.push({ err: i > 0, text: `… ${s.skipped} bytes not shown …\n` });
          // The server sends whole lines while the command runs; a finished
          // command's last line may have no newline — end it here.
          if (s.text) add.push({ err: i > 0, text: s.text.endsWith('\n') || r.running ? s.text : s.text + '\n' });
        });
        if (add.length) {
          setLines(prev => {
            const next = prev.concat(add);
            let total = next.reduce((n, l) => n + l.text.length, 0);
            while (total > KEEP_CHARS && next.length > 1) total -= next.shift()!.text.length;
            return next;
          });
        }
        // A finished command has nothing more to say: one last read, then stop.
        if (!r.running && !running.current) return;
      } catch (e: any) {
        if (!live) return;
        // A job being collected can miss one read: give up only after a few.
        if (String(e?.message || '').startsWith('404') && ++misses >= 3) { setGone(true); return; }
      }
      if (live) timer = window.setTimeout(tick, document.hidden ? LOG_POLL_MS * 5 : LOG_POLL_MS);
    }
    tick();
    return () => { live = false; window.clearTimeout(timer); };
  }, [sessionId, proc.id]);

  useEffect(() => {
    if (follow && body.current) body.current.scrollTop = body.current.scrollHeight;
  }, [lines, follow]);

  const st = stateOf(proc);
  return (
    <div className="proc-tile" style={{
      gridColumn: wide ? '1 / -1' : undefined,
      display: 'flex', flexDirection: 'column', minWidth: 0,
      border: '1px solid var(--border-1)', borderRadius: 6, background: 'var(--bg-0)',
    }}>
      <div className="row" style={{ gap: 6, alignItems: 'center', padding: '4px 8px',
                                    borderBottom: '1px solid var(--border-1)' }}>
        <span className={st.cls}>{st.text}</span>
        <span title={`${proc.cmd}${proc.cwd ? `\n in ${proc.cwd}` : ''}`}
              style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
                       whiteSpace: 'nowrap', fontWeight: 600 }}>
          {title(proc)}
        </span>
        <span className="muted xs">{since(proc.started_at, proc.running ? undefined : proc.ended_at)}</span>
        <button type="button" className={`ghost sm${follow ? ' active' : ''}`}
                title={follow ? 'Following the end — click to stop scrolling' : 'Follow the end'}
                onClick={() => setFollow(f => !f)}>⤓</button>
        <button type="button" className="ghost sm" title="Clear this view (the command is not touched)"
                onClick={() => setLines([])}>⌫</button>
        <button type="button" className="ghost sm" title={wide ? 'Back to a tile' : 'Full width'}
                onClick={() => setWide(w => !w)}>{wide ? '▭' : '⬌'}</button>
        {proc.running && (
          <button type="button" className="ghost sm" title="Stop this command"
                  onClick={() => { chatProcessKill(sessionId, proc.id).catch(() => undefined); }}>■</button>
        )}
        <button type="button" className="ghost sm" title="Close this log" onClick={onClose}>✕</button>
      </div>
      <pre ref={body}
           onScroll={e => {
             const el = e.currentTarget;
             const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
             if (atEnd !== follow) setFollow(atEnd);
           }}
           style={{ margin: 0, padding: '6px 8px', flex: 'none', overflow: 'auto', height: wide ? 360 : 220,
                    border: 0, borderRadius: '0 0 6px 6px',
                    fontSize: 12, lineHeight: 1.35, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        {lines.length === 0
          ? <span className="muted">{gone ? 'This command is gone — its output was not kept.' : 'No output yet.'}</span>
          : lines.map((l, i) => (
              <span key={i} style={l.err ? { color: 'var(--err)' } : undefined}>{l.text}</span>
            ))}
      </pre>
    </div>
  );
}

/** The chat's running commands at the foot of the chat. Click one to tile its
 *  live log; the agent's show_logs opens a tile by itself. */
export function ProcessDock({ sessionId }: { sessionId: number | null }) {
  const [procs, setProcs] = useState<ChatProcess[]>([]);
  const [open, setOpen] = useState<string[]>([]);
  const [expanded, setExpanded] = useState(true);
  const seenShown = useRef(0);
  const first = useRef(true);

  useEffect(() => {
    setProcs([]);
    setOpen([]);
    seenShown.current = 0;
    first.current = true;
    if (sessionId === null) return;
    let live = true;
    let timer: number | undefined;
    async function tick() {
      try {
        const r = await chatProcesses(sessionId!);
        if (!live) return;
        setProcs(r.processes);
        const top = Math.max(0, ...r.processes.map(p => p.shown || 0));
        if (first.current || top < seenShown.current) {
          // First look, or the server restarted: asks from before are not replayed.
          first.current = false;
          seenShown.current = top;
        } else if (top > seenShown.current) {
          const fresh = r.processes.filter(p => p.shown > seenShown.current).map(p => p.id);
          seenShown.current = top;
          setOpen(o => o.concat(fresh.filter(id => !o.includes(id))));
          setExpanded(true);
        }
      } catch { /* a missed poll is retried */ }
      if (live) timer = window.setTimeout(tick, document.hidden ? LIST_POLL_MS * 5 : LIST_POLL_MS);
    }
    tick();
    return () => { live = false; window.clearTimeout(timer); };
  }, [sessionId]);

  if (sessionId === null || procs.length === 0) return null;
  const running = procs.filter(p => p.running).length;
  const tiles = open.map(id => procs.find(p => p.id === id)).filter(Boolean) as ChatProcess[];
  const toggle = (id: string) =>
    setOpen(o => o.includes(id) ? o.filter(x => x !== id) : o.concat(id));

  return (
    <div className="proc-dock" style={{ borderTop: '1px solid var(--border-1)' }}>
      <div className="row" style={{ gap: 6, flexWrap: 'wrap', alignItems: 'center', padding: '6px 10px' }}>
        <button type="button" className="ghost sm" onClick={() => setExpanded(e => !e)}
                title={expanded ? 'Hide the logs' : 'Show the open logs'}>
          {expanded ? '▾' : '▸'} Processes
        </button>
        <span className="muted xs">{running ? `${running} running` : 'none running'}</span>
        {procs.map(p => {
          const st = stateOf(p);
          const on = open.includes(p.id);
          return (
            <span key={p.id} className={st.cls}
                  style={{ maxWidth: 280, outline: on ? '1px solid currentColor' : undefined }}>
              <button type="button"
                      title={`${p.cmd}\n${st.text} · ${since(p.started_at, p.running ? undefined : p.ended_at)}${on ? '\nClick to close its log' : '\nClick to open its log'}`}
                      onClick={() => { toggle(p.id); setExpanded(true); }}
                      style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                               color: 'inherit', font: 'inherit', overflow: 'hidden',
                               textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 260 }}>
                {p.running ? '● ' : ''}{title(p)}
              </button>
            </span>
          );
        })}
        {tiles.length > 1 && expanded && (
          <button type="button" className="ghost sm" onClick={() => setOpen([])}>close all</button>
        )}
      </div>
      {expanded && tiles.length > 0 && (
        <div style={{ display: 'grid', gap: 8, padding: '0 10px 8px',
                      gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))',
                      maxHeight: '45vh', overflow: 'auto' }}>
          {tiles.map(p => (
            <LogTile key={p.id} sessionId={sessionId} proc={p} onClose={() => toggle(p.id)} />
          ))}
        </div>
      )}
    </div>
  );
}
