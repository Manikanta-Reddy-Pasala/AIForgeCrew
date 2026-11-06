import { useEffect, useState, type ReactNode } from 'react';
import { chatOverview, ChatOverview, ChatDecision, chatBackground, ChatBackground,
         ChatBackgroundItem, chatBackgroundStop } from '../api';
import { AgentStep, SubtaskItem } from './Chat.types';
import { describeStep } from '../stepDescribe';

// ── Run overview — progress beside the chat ──────────────────────────────────
// What runs now and how far the task list got, at a glance. Your own
// messages (steers included) stay in the chat as your bubbles: the panel
// never repeats them, nor goals worded from them.
//
// The agent's lines are sorted by what they are about: a note on a task of
// the list shows under that task (newest only, the rest on a click); a change
// to the list (added, skipped, failed, left) shows under "Agent changes".
// Lines of an older turn, or about a task no longer on the list (the list was
// redone), are history: folded under "Earlier", never mixed with the live run.

const STATUS_ICON: Record<string, string> = {
  done: '✓', won: '✓', running: '', failed: '✕', skipped: '–', cancelled: '–',
  left: '!', pending: '', planned: '',
};
const CLOSED = new Set(['done', 'won', 'skipped', 'failed', 'cancelled', 'left']);
const POLL_MS = 4000;
// Background work (a watch, a scheduled job) runs with no turn: always polled,
// often while some is there, rarely while there is none; never in a hidden tab.
const BG_POLL_MS = 5000;
const BG_IDLE_POLL_MS = 30000;
const BG_ICON: Record<string, string> = { watch: '👁', command: '▶', schedule: '⏱' };
const BG_KIND: Record<string, string> = { watch: 'Watch', command: 'Command', schedule: 'Scheduled' };
const TICK_MS = 30000;
const CHANGES_SHOWN = 4;
// Lines worded from the user's message (goal changes, a question on it) are
// the chat's to show; the panel keeps what the agent did to the plan.
const OWN_KINDS = new Set(['plan', 'skip', 'fail', 'left', 'choice']);
const KIND: Record<string, { icon: string; label: string }> = {
  plan: { icon: '+', label: 'Task list changed' },
  skip: { icon: '–', label: 'Skipped' },
  fail: { icon: '✕', label: 'Failed' },
  left: { icon: '!', label: 'Left undone' },
  choice: { icon: '•', label: 'Agent note' },
};

/** "now", "4m", "2h", else the date: how old a line is, at a glance. */
function ago(t: number, now: number): string {
  const s = Math.max(0, Math.round(now / 1000 - t));
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(t * 1000).toLocaleDateString([], { day: 'numeric', month: 'short' });
}

function stamp(t: number): string {
  return new Date(t * 1000).toLocaleString([], { hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'short' });
}

// Rows written before the slug field: the task is in the words.
const SLUG_IN_TEXT = [/\(([\w.-]+)\)\s*$/, /^Agent marked ([\w.-]+) /, /^Agent took ([\w.-]+) off/,
                      /of its own — ([\w.-]+):/];
function slugOf(d: ChatDecision): string | undefined {
  if (d.slug) return d.slug;
  for (const re of SLUG_IN_TEXT) {
    const m = re.exec(d.text);
    if (m) return m[1];
  }
  return undefined;
}

/** A note's words without the "(slug)" tail — it shows under that task.
 *  Plain string work: the slug is the model's own and may hold any sign. */
function noteText(d: ChatDecision): string {
  const slug = slugOf(d);
  const t = d.text.trimEnd();
  const tail = ` (${slug})`;
  return slug && t.endsWith(tail) ? t.slice(0, -tail.length) : d.text;
}

// The newest thing the run did, in words: a tool row says what it does.
function lastAction(steps: AgentStep[] | undefined): string {
  for (const s of [...(steps ?? [])].reverse()) {
    if (s.kind === 'tool' && s.name !== 'plan_progress') {
      return s.description || describeStep(s.name, s.args as Record<string, unknown>);
    }
  }
  return '';
}

// A task's name only: the "— check: …" the planner appends shows on hover.
function shortTitle(s: SubtaskItem): string {
  const full = s.title || s.goal || s.slug;
  return full.split(/\s+[—-]\s+check:/i)[0] || full;
}

type Sorted = { notes: Map<string, ChatDecision[]>; changes: ChatDecision[]; earlier: ChatDecision[] };

/** Newest first: notes per task of the list, this turn's changes, history. */
export function sortDecisions(rows: ChatDecision[], turn: number | null | undefined,
                              items: SubtaskItem[]): Sorted {
  const onList = new Set(items.map(s => s.slug));
  const isThisTurn = (d: ChatDecision) => turn == null || d.turn === turn;
  // Tasks the agent itself added or took off this turn: a line about one is
  // that change (the why of a removal), not a leftover of an older list.
  const changed = new Set(rows.filter(d => d.kind === 'plan' && isThisTurn(d))
    .map(slugOf).filter((x): x is string => !!x));
  const notes = new Map<string, ChatDecision[]>();
  const changes: ChatDecision[] = [];
  const earlier: ChatDecision[] = [];
  for (const d of [...rows].reverse()) {
    if (d.by === 'you' || !OWN_KINDS.has(d.kind)) continue;
    const slug = slugOf(d);
    const thisTurn = isThisTurn(d);
    // A line about a task the list no longer has: the list was redone since.
    const gone = !!slug && items.length > 0 && !onList.has(slug) && d.kind !== 'plan'
      && !changed.has(slug);
    if (!thisTurn || gone) earlier.push(d);
    else if (d.kind === 'choice' && slug && onList.has(slug)) {
      notes.set(slug, [...(notes.get(slug) ?? []), d]);
    } else changes.push(d);
  }
  return { notes, changes, earlier };
}

function Section({ title, extra, children }: Readonly<{
  title: string; extra?: ReactNode; children: ReactNode;
}>) {
  return (
    <section className="run-ov-section">
      <div className="run-ov-title"><span>{title}</span>{extra}</div>
      {children}
    </section>
  );
}

function ChangeRow({ d, now, old }: Readonly<{ d: ChatDecision; now: number; old?: boolean }>) {
  const [open, setOpen] = useState(false);
  const k = KIND[d.kind] ?? KIND.choice;
  return (
    <li className={`run-ov-change kind-${d.kind}${old ? ' is-old' : ''}`}>
      <span className="run-ov-kind" role="img" title={k.label} aria-label={k.label}>{k.icon}</span>
      <button type="button" className={`run-ov-text${open ? ' open' : ''}`} aria-expanded={open}
              onClick={() => setOpen(v => !v)} title={open ? undefined : d.text}>
        <span className="run-ov-clamp">{d.text}</span>
      </button>
      <span className="run-ov-ago" title={stamp(d.t)}>{ago(d.t, now)}</span>
    </li>
  );
}

function TaskRow({ s, notes, now }: Readonly<{ s: SubtaskItem; notes: ChatDecision[]; now: number }>) {
  const [open, setOpen] = useState(false);
  const latest = notes[0];
  return (
    <li className={`run-ov-task st-${s.status}`}>
      <span className="run-ov-dot" role="img" aria-label={s.status}>{STATUS_ICON[s.status] ?? ''}</span>
      <div className="run-ov-task-body">
        <div className="run-ov-task-title" title={s.title || s.goal || s.slug}>
          {shortTitle(s)}
          {s.self_added && <span className="run-ov-tag" title="The agent added this itself — nobody asked for it">agent added</span>}
          {s.status === 'left' && <span className="run-ov-tag warn">not done</span>}
        </div>
        {latest && (
          <button type="button" className={`run-ov-note${open ? ' open' : ''}`} onClick={() => setOpen(v => !v)}
                  aria-expanded={open}
                  title={open ? 'Show less' : 'Show the whole note'}>
            <span className="run-ov-clamp">
              {noteText(latest)} <span className="run-ov-ago" title={stamp(latest.t)}>· {ago(latest.t, now)}</span>
              {!open && notes.length > 1 && <span className="run-ov-more"> +{notes.length - 1}</span>}
            </span>
          </button>
        )}
        {open && notes.slice(1).map((d, i) => (
          <div key={`${d.t}:${i}`} className="run-ov-note old">
            {noteText(d)} <span className="run-ov-ago" title={stamp(d.t)}>· {ago(d.t, now)}</span>
          </div>
        ))}
      </div>
    </li>
  );
}

/** "14:05", or the date and time when it is not today. */
function whenAt(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);       // the server sends its offset
  if (Number.isNaN(d.getTime())) return '';
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString([], { hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'short' });
}

/** A title with `code` spans rendered as code. */
function withCode(text: string): ReactNode {
  return text.split('`').map((part, i) => (i % 2 ? <code key={i}>{part}</code> : part));
}

function BackgroundRow({ it }: Readonly<{ it: ChatBackgroundItem }>) {
  const on = it.state === 'running';
  let when = '';
  if (on) when = it.kind === 'schedule' ? 'running now' : 'running';
  else if (it.next_run_at) when = `next ${whenAt(it.next_run_at)}`;
  return (
    <li className={`run-ov-bg ${on ? 'on' : ''}`} title={(it.cron ? `${it.title} — ${it.cron}` : it.title).replaceAll('`', '')}>
      {on ? <span className="run-ov-pulse" aria-hidden="true" />
        : <span className="run-ov-bg-icon" aria-hidden="true">{BG_ICON[it.kind] ?? '•'}</span>}
      <span className="sr-only">{BG_KIND[it.kind] ?? 'Background'}: </span>
      <span className="run-ov-bg-title">{withCode(it.title)}{it.cron && <span className="run-ov-ago"> ({it.cron})</span>}</span>
      {when && <span className="run-ov-ago">· {when}</span>}
      {it.last_error && <div className="run-ov-note old">last error: {it.last_error}</div>}
    </li>
  );
}

function NowCard({ running, next, busy, awaiting, items, action, phase, bgBusy }: Readonly<{
  running?: SubtaskItem; next?: SubtaskItem; busy: boolean; awaiting?: boolean;
  items: SubtaskItem[]; action: string; phase?: string;
  /** the first busy background item, when no turn runs */
  bgBusy?: ChatBackgroundItem;
}>) {
  if (awaiting) {
    return <div className="run-ov-now ask"><div className="run-ov-now-title">Waiting for your reply in the chat</div></div>;
  }
  if (!busy && bgBusy) {
    // No turn holds the chat, but its watch or scheduled run is working.
    return (
      <div className="run-ov-now">
        <div className="run-ov-now-head">
          <span className="run-ov-pulse" aria-hidden="true" />
          <span className="run-ov-now-label">Working in the background</span>
        </div>
        <div className="run-ov-now-title" title={bgBusy.title.replaceAll('`', '')}>{withCode(bgBusy.title)}</div>
      </div>
    );
  }
  if (!busy) {
    // A task still marked running when nothing runs: the run stopped there.
    return (
      <div className="run-ov-now idle">
        {running && <div className="run-ov-now-label idle">Stopped at task {items.indexOf(running) + 1} of {items.length}</div>}
        <div className="run-ov-now-title">{running ? shortTitle(running) : 'Nothing running'}</div>
      </div>
    );
  }
  const task = running ?? next;
  return (
    <div className="run-ov-now">
      <div className="run-ov-now-head">
        <span className="run-ov-pulse" aria-hidden="true" />
        <span className="run-ov-now-label">
          {running ? `Task ${items.indexOf(running) + 1} of ${items.length}` : task ? 'Next up' : 'Starting'}
        </span>
      </div>
      {task && <div className="run-ov-now-title" title={task.title || task.goal}>{shortTitle(task)}</div>}
      {action && <div className="run-ov-now-step"><span>Last step</span>{action}</div>}
      {phase && <div className="run-ov-now-wait">⏳ {phase}</div>}
    </div>
  );
}

export function RunOverview({ sessionId, busy, awaiting, subtasks, steps, phase, onClose,
                             onBackground }: Readonly<{
  sessionId: number;
  busy: boolean;
  /** the run asked something and waits for the user's answer */
  awaiting?: boolean;
  /** the turn's task list (live, else the last turn's) */
  subtasks?: SubtaskItem[];
  /** the live turn's steps, for what it does right now */
  steps?: AgentStep[];
  /** what the run waits on while it is quiet (the heartbeat's phase) */
  phase?: string;
  onClose: () => void;
  /** whether background work is busy now (the chat shows as working) */
  onBackground?: (active: boolean) => void;
}>) {
  const [ov, setOv] = useState<ChatOverview | null>(null);
  const [bg, setBg] = useState<ChatBackground | null>(null);
  const [stopping, setStopping] = useState(false);
  const [allChanges, setAllChanges] = useState(false);
  const [showEarlier, setShowEarlier] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  // Another chat: nothing of the last one's stays on screen.
  useEffect(() => { setOv(null); setBg(null); setStopping(false); setAllChanges(false); setShowEarlier(false); }, [sessionId]);

  const bgCount = bg?.items.length ?? 0;
  useEffect(() => {
    let live = true;
    const load = () => {
      if (typeof document !== 'undefined' && document.hidden) return;
      chatBackground(sessionId)
        .then(r => { if (live) setBg(r); })
        .catch(() => { /* the panel keeps what it had */ });
    };
    load();
    const h = setInterval(load, bgCount > 0 || busy ? BG_POLL_MS : BG_IDLE_POLL_MS);
    return () => { live = false; clearInterval(h); };
  }, [sessionId, busy, bgCount]);

  const bgActive = !!bg?.active;
  useEffect(() => { onBackground?.(bgActive); }, [bgActive, onBackground]);
  // The panel closed: it no longer knows, so the chat stops saying so.
  useEffect(() => () => onBackground?.(false), [onBackground]);

  useEffect(() => {
    let live = true;
    const load = () => chatOverview(sessionId)
      .then(r => { if (live) { setOv(r); setNow(Date.now()); } })
      .catch(() => { /* the panel keeps what it had */ });
    load();
    // Polled while the run goes on; once more when it ends (busy flips).
    const h = busy ? setInterval(load, POLL_MS) : undefined;
    return () => { live = false; if (h) clearInterval(h); };
  }, [sessionId, busy]);

  // "4m ago" keeps counting while nothing new comes in.
  useEffect(() => {
    const h = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(h);
  }, []);

  const items = subtasks ?? [];
  const running = items.find(s => s.status === 'running');
  // No item marked running: the next open one is what comes up, not "now".
  const next = running || !busy ? undefined
    : items.find(s => !CLOSED.has(s.status) && s.status !== 'planned');
  const closed = items.filter(s => CLOSED.has(s.status) && s.status !== 'left').length;
  const pct = items.length ? Math.round((closed / items.length) * 100) : 0;
  const rows = ov?.decisions ?? [];
  const { notes, changes, earlier } = sortDecisions(rows, ov?.turn, items);
  // Keys from the row itself (time, kind, words), not its place: a new row,
  // or the oldest one dropped at the store's cap, remounts nothing, so an
  // opened line stays open across polls. Same row twice gets a counter.
  const seen = new Map<string, number>();
  const keyOf = new Map(rows.map(d => {
    const base = `${d.t}|${d.kind}|${d.text}`;
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return [d, `${base}|${n}`];
  }));
  const shownChanges = allChanges ? changes : changes.slice(0, CHANGES_SHOWN);
  const action = busy ? lastAction(steps) : '';

  let state = 'Idle';
  let stateCls = '';
  if (awaiting) { state = 'Waiting for you'; stateCls = 'ask'; }
  else if (busy) { state = 'Running'; stateCls = 'on'; }
  else if (bgActive) { state = 'Running in background'; stateCls = 'on'; }
  const bgItems = bg?.items ?? [];
  const stopBg = () => {
    const sid = sessionId;
    setStopping(true);
    chatBackgroundStop(sid)
      .then(r => { if (sid === sessionId) setBg(r); })
      .catch(() => { /* the next poll shows what is left */ })
      .finally(() => setStopping(false));
  };

  return (
    <aside className="run-overview" aria-label="Run overview">
      <div className="run-ov-head">
        <strong>Progress</strong>
        <span className={`run-ov-state ${stateCls}`}>{state}</span>
        <button type="button" className="ghost xs" onClick={onClose} aria-label="Hide the run overview"
                title="Hide the run overview" style={{ marginLeft: 'auto', padding: '0 6px' }}>✕</button>
      </div>

      <div className="run-ov-body">
        <NowCard running={running} next={next} busy={busy} awaiting={awaiting} items={items}
                 action={action} phase={busy ? phase : undefined}
                 bgBusy={bgItems.find(i => i.state === 'running')} />

        {bgItems.length > 0 && (
          <Section title="Background"
                   extra={bgActive && !busy && (
                     <button type="button" className="ghost xs run-ov-link" onClick={stopBg} disabled={stopping}
                             aria-label="Stop background work"
                             title="Stop this chat's watches, background commands and the scheduled run in flight (the schedule itself stays; nothing else of the chat stops)">
                       {stopping ? 'stopping…' : 'stop'}
                     </button>
                   )}>
            <ul className="run-ov-changes">
              {bgItems.map(it => <BackgroundRow key={it.id} it={it} />)}
            </ul>
          </Section>
        )}

        <Section title="Tasks" extra={items.length > 0 && <span className="run-ov-count">{closed}/{items.length} · {pct}%</span>}>
          {items.length === 0 ? (
            <div className="run-ov-empty">No task list for this turn.</div>
          ) : (
            <>
              <div className="run-ov-bar" role="progressbar" aria-label="Tasks done" aria-valuenow={pct}
                   aria-valuemin={0} aria-valuemax={100} aria-valuetext={`${closed} of ${items.length} tasks`}>
                {items.map(s => <span key={s.slug} className={`st-${s.status}`} title={`${shortTitle(s)} — ${s.status}`} />)}
              </div>
              <ol className="run-ov-tasks">
                {items.map(s => <TaskRow key={s.slug} s={s} notes={notes.get(s.slug) ?? []} now={now} />)}
              </ol>
            </>
          )}
        </Section>

        <Section title="Agent changes"
                 extra={changes.length > CHANGES_SHOWN && (
                   <button type="button" className="ghost xs run-ov-link" onClick={() => setAllChanges(v => !v)}>
                     {allChanges ? 'fewer' : `all ${changes.length}`}
                   </button>
                 )}>
          {changes.length === 0 ? (
            <div className="run-ov-empty">None this turn. A task the agent adds, skips or leaves undone shows here.</div>
          ) : (
            <ul className="run-ov-changes">
              {shownChanges.map(d => <ChangeRow key={keyOf.get(d)} d={d} now={now} />)}
            </ul>
          )}
          {earlier.length > 0 && (
            <div className="run-ov-earlier">
              <button type="button" className="ghost xs run-ov-link" onClick={() => setShowEarlier(v => !v)}
                      aria-expanded={showEarlier} title="Lines of older turns, or of a task list the agent has since redone">
                {showEarlier ? '▾' : '▸'} Earlier ({earlier.length})
              </button>
              {showEarlier && (
                <ul className="run-ov-changes">
                  {earlier.map(d => <ChangeRow key={keyOf.get(d)} d={d} now={now} old />)}
                </ul>
              )}
            </div>
          )}
        </Section>
      </div>
    </aside>
  );
}
