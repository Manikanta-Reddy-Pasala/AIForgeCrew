import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  chatProcesses, chatProcessLog, chatProcessFile, chatProcessKill, chatProcessDownload,
  ChatProcess, ChatProcessChunk,
} from '../api';

const LIST_POLL_MS = 2000;
const LOG_POLL_MS = 1000;
/** Characters kept per open log (the oldest are dropped first). */
const KEEP_CHARS = 200_000;
/** First read of a tile: the last 64 KB. */
const FIRST_READ = -64 * 1024;
/** The pop-up view: the last 256 KB. */
const VIEW_READ = -256 * 1024;

type Line = { err: boolean; text: string };
/** What a tile shows: the command's own output, or one of its log files. */
type Source = { proc: string; path?: string };

const tileKey = (s: Source) => (s.path ? `file:${s.proc}:${s.path}` : `out:${s.proc}`);
const baseName = (p: string) => p.split('/').pop() || p;

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

/** One read of a source from `offsets` on: the chunks per stream, and whether
 *  it is still being written. */
async function readSource(sessionId: number, src: Source, offsets: number[], first: number):
    Promise<{ streams: ChatProcessChunk[]; running: boolean }> {
  if (src.path) {
    const r = await chatProcessFile(sessionId, src.proc, src.path,
                                    offsets.length ? offsets[0] : first, Math.abs(first));
    return { streams: [r], running: r.running };
  }
  const r = await chatProcessLog(sessionId, src.proc, offsets.length ? offsets : [first, first],
                                 Math.abs(first));
  return { streams: r.streams, running: r.running };
}

function toLines(streams: ChatProcessChunk[], running: boolean): Line[] {
  const add: Line[] = [];
  streams.forEach((s, i) => {
    if (s.skipped) add.push({ err: i > 0, text: `… ${s.skipped} bytes not shown …\n` });
    // Whole lines arrive while it runs; a finished log's last line may lack its newline.
    if (s.text) add.push({ err: i > 0, text: s.text.endsWith('\n') || running ? s.text : s.text + '\n' });
  });
  return add;
}

function download(sessionId: number, src: Source) {
  chatProcessDownload(sessionId, src.proc, src.path)
    .catch((e: any) => { toast.error(`Download failed: ${e?.message || e}`); });
}

const btnReset = {
  background: 'none', border: 'none', padding: 0, cursor: 'pointer', color: 'inherit',
  font: 'inherit', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
} as const;

function LogBody({ lines, height, follow, setFollow, empty }: {
  lines: Line[]; height: number | string; follow: boolean;
  setFollow: (f: boolean) => void; empty: string;
}) {
  const body = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (follow && body.current) body.current.scrollTop = body.current.scrollHeight;
  }, [lines, follow]);
  return (
    <pre ref={body}
         onScroll={e => {
           const el = e.currentTarget;
           const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
           if (atEnd !== follow) setFollow(atEnd);
         }}
         style={{ margin: 0, padding: '6px 8px', flex: 'none', overflow: 'auto', height,
                  width: '100%', boxSizing: 'border-box',
                  border: 0, borderRadius: '0 0 6px 6px',
                  fontSize: 12, lineHeight: 1.35, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
      {lines.length === 0
        ? <span className="muted">{empty}</span>
        : lines.map((l, i) => (
            <span key={i} style={l.err ? { color: 'var(--err)' } : undefined}>{l.text}</span>
          ))}
    </pre>
  );
}

/** A bigger look at a log: its last 256 KB, refreshed on demand, downloadable. */
function LogViewer({ sessionId, src, name, onClose }: {
  sessionId: number; src: Source; name: string; onClose: () => void;
}) {
  const [lines, setLines] = useState<Line[]>([]);
  const [follow, setFollow] = useState(true);
  const [err, setErr] = useState('');
  async function load() {
    try {
      const r = await readSource(sessionId, src, [], VIEW_READ);
      setLines(toLines(r.streams, false));
      setErr('');
    } catch (e: any) { setErr(String(e?.message || e)); }
  }
  useEffect(() => {
    load();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, tileKey(src)]);
  return (
    <div role="dialog" aria-label={`Log: ${name}`} onClick={onClose}
         style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,.45)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div className="proc-viewer" onClick={e => e.stopPropagation()}
           style={{ width: 'min(1100px, 92vw)', height: '80vh', display: 'flex', flexDirection: 'column',
                    background: 'var(--bg-1)', border: '1px solid var(--border-1)', borderRadius: 8 }}>
        <div className="row" style={{ gap: 6, alignItems: 'center', padding: '6px 10px',
                                      borderBottom: '1px solid var(--border-1)' }}>
          <span style={{ flex: 1, minWidth: 0, fontWeight: 600, overflow: 'hidden',
                         textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={src.path || name}>
            {name}
          </span>
          <span className="muted xs">last 256 KB</span>
          <button type="button" className="ghost sm" onClick={load} title="Read it again">↻ Refresh</button>
          <button type="button" className="ghost sm" onClick={() => download(sessionId, src)}
                  title="Download the whole log">⬇ Download</button>
          <button type="button" className="ghost sm" onClick={onClose} title="Close (Esc)">✕</button>
        </div>
        {err && <div className="xs" style={{ color: 'var(--err)', padding: '4px 10px' }}>{err}</div>}
        <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
          <LogBody lines={lines} height="100%" follow={follow} setFollow={setFollow} empty="Empty." />
        </div>
      </div>
    </div>
  );
}

/** One live log tile: tails a source by byte offset, newest at the bottom. */
function LogTile({ sessionId, proc, src, onClose, onView }: {
  sessionId: number;
  proc: ChatProcess;
  src: Source;
  onClose: () => void;
  onView: () => void;
}) {
  const [lines, setLines] = useState<Line[]>([]);
  const [follow, setFollow] = useState(true);
  const [wide, setWide] = useState(false);
  const [gone, setGone] = useState(false);
  const offsets = useRef<number[]>([]);
  const running = useRef(proc.running);
  running.current = proc.running;
  const key = tileKey(src);

  useEffect(() => {
    let live = true;
    let timer: number | undefined;
    let misses = 0;
    offsets.current = [];
    setLines([]);
    setGone(false);
    async function tick() {
      try {
        const r = await readSource(sessionId, src, offsets.current, FIRST_READ);
        if (!live) return;
        misses = 0;
        offsets.current = r.streams.map(s => s.next);
        const add = toLines(r.streams, r.running);
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, key]);

  const st = stateOf(proc);
  const name = src.path ? `📄 ${baseName(src.path)}` : title(proc);
  return (
    <div className="proc-tile" style={{
      gridColumn: wide ? '1 / -1' : undefined,
      display: 'flex', flexDirection: 'column', minWidth: 0,
      border: '1px solid var(--border-1)', borderRadius: 6, background: 'var(--bg-0)',
    }}>
      <div className="row" style={{ gap: 6, alignItems: 'center', padding: '4px 8px',
                                    borderBottom: '1px solid var(--border-1)' }}>
        <span className={st.cls}>{st.text}</span>
        <span title={src.path ? `${src.path}\nwritten by: ${proc.cmd}` : `${proc.cmd}${proc.cwd ? `\n in ${proc.cwd}` : ''}`}
              style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
                       whiteSpace: 'nowrap', fontWeight: 600 }}>
          {name}
        </span>
        <span className="muted xs">{since(proc.started_at, proc.running ? undefined : proc.ended_at)}</span>
        <button type="button" className={`ghost sm${follow ? ' active' : ''}`}
                title={follow ? 'Following the end — click to stop scrolling' : 'Follow the end'}
                onClick={() => setFollow(f => !f)}>⤓</button>
        <button type="button" className="ghost sm" title="Clear this view (the log is not touched)"
                onClick={() => setLines([])}>⌫</button>
        <button type="button" className="ghost sm" title={wide ? 'Back to a tile' : 'Full width'}
                onClick={() => setWide(w => !w)}>{wide ? '▭' : '⬌'}</button>
        <button type="button" className="ghost sm" title="Open in a bigger view" onClick={onView}>⤢</button>
        <button type="button" className="ghost sm" title="Download the whole log"
                onClick={() => download(sessionId, src)}>⬇</button>
        {proc.running && !src.path && (
          <button type="button" className="ghost sm" title="Stop this command (Ctrl-C, then force kill)"
                  onClick={() => { chatProcessKill(sessionId, proc.id).catch(() => undefined); }}>■</button>
        )}
        <button type="button" className="ghost sm" title="Close this log" onClick={onClose}>✕</button>
      </div>
      <LogBody lines={lines} height={wide ? 360 : 220} follow={follow} setFollow={setFollow}
               empty={gone ? 'This log is gone — its output was not kept.' : 'No output yet.'} />
    </div>
  );
}

/** The chat's commands at the foot of the chat. Click one to tile its live
 *  output; click one of its log files to tile that file. The agent's
 *  show_logs opens a tile by itself. */
export function ProcessDock({ sessionId }: { sessionId: number | null }) {
  const [procs, setProcs] = useState<ChatProcess[]>([]);
  const [open, setOpen] = useState<Source[]>([]);
  const [viewing, setViewing] = useState<Source | null>(null);
  const [expanded, setExpanded] = useState(true);
  const seenShown = useRef(0);
  const first = useRef(true);

  useEffect(() => {
    setProcs([]);
    setOpen([]);
    setViewing(null);
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
          const fresh = r.processes.filter(p => p.shown > seenShown.current).map(p => ({ proc: p.id }));
          seenShown.current = top;
          setOpen(o => o.concat(fresh.filter(f => !o.some(x => tileKey(x) === tileKey(f)))));
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
  const byId = new Map(procs.map(p => [p.id, p]));
  const tiles = open.filter(s => byId.has(s.proc));
  const isOpen = (s: Source) => open.some(x => tileKey(x) === tileKey(s));
  const toggle = (s: Source) => {
    setOpen(o => (o.some(x => tileKey(x) === tileKey(s))
      ? o.filter(x => tileKey(x) !== tileKey(s)) : o.concat(s)));
    setExpanded(true);
  };
  const withFiles = procs.filter(p => (p.log_files || []).length > 0);
  const viewProc = viewing ? byId.get(viewing.proc) : undefined;

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
          const on = isOpen({ proc: p.id });
          return (
            <span key={p.id} className={st.cls}
                  style={{ maxWidth: 300, outline: on ? '1px solid currentColor' : undefined }}>
              <button type="button"
                      title={`${p.cmd}\n${st.text} · ${since(p.started_at, p.running ? undefined : p.ended_at)}${on ? '\nClick to close its output' : '\nClick to watch its output'}`}
                      onClick={() => toggle({ proc: p.id })} style={{ ...btnReset, maxWidth: 240 }}>
                {p.running ? '● ' : ''}{title(p)}
              </button>
              <button type="button" title="Open its output in a bigger view"
                      onClick={() => setViewing({ proc: p.id })} style={{ ...btnReset, opacity: 0.75 }}>⤢</button>
            </span>
          );
        })}
        {running > 0 && (
          <button type="button" className="ghost sm"
                  title="Stop every command this chat started (Ctrl-C, then force kill)"
                  onClick={() => {
                    procs.filter(p => p.running)
                      .forEach(p => { chatProcessKill(sessionId, p.id).catch(() => undefined); });
                  }}>■ Stop all</button>
        )}
        {tiles.length > 1 && expanded && (
          <button type="button" className="ghost sm" onClick={() => setOpen([])}>close all</button>
        )}
      </div>
      {withFiles.length > 0 && (
        <div className="row" style={{ gap: 6, flexWrap: 'wrap', alignItems: 'center', padding: '0 10px 6px' }}>
          <span className="muted xs">Log files</span>
          {withFiles.map(p => p.log_files.map(f => {
            const src = { proc: p.id, path: f };
            const on = isOpen(src);
            return (
              <span key={`${p.id}:${f}`} className={`chip${p.running ? ' info' : ''}`}
                    style={{ maxWidth: 300, outline: on ? '1px solid currentColor' : undefined }}>
                <button type="button" onClick={() => toggle(src)} style={{ ...btnReset, maxWidth: 220 }}
                        title={`${f}\nfrom: ${title(p)}${p.running ? '' : ' (ended)'}\nClick to ${on ? 'close' : 'watch'} it`}>
                  📄 {baseName(f)}
                </button>
                <button type="button" title="Open in a bigger view" onClick={() => setViewing(src)}
                        style={{ ...btnReset, opacity: 0.75 }}>⤢</button>
                <button type="button" title="Download this file" onClick={() => download(sessionId, src)}
                        style={{ ...btnReset, opacity: 0.75 }}>⬇</button>
              </span>
            );
          }))}
        </div>
      )}
      {expanded && tiles.length > 0 && (
        <div style={{ display: 'grid', gap: 8, padding: '0 10px 8px',
                      gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))',
                      maxHeight: '45vh', overflow: 'auto' }}>
          {tiles.map(s => (
            <LogTile key={tileKey(s)} sessionId={sessionId} proc={byId.get(s.proc)!} src={s}
                     onClose={() => toggle(s)} onView={() => setViewing(s)} />
          ))}
        </div>
      )}
      {viewing && viewProc && (
        <LogViewer sessionId={sessionId} src={viewing} onClose={() => setViewing(null)}
                   name={viewing.path ? baseName(viewing.path) : title(viewProc)} />
      )}
    </div>
  );
}
