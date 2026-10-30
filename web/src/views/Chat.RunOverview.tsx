import { useEffect, useState, type ReactNode } from 'react';
import { chatOverview, ChatOverview, ChatDecision } from '../api';
import { AgentStep, SubtaskItem } from './Chat.types';
import { describeStep } from '../stepDescribe';

// ── Run overview — the whole picture beside the chat ─────────────────────────
// The task dock says which item runs; the steps say which file is read. What
// the run is FOR (the goals), how far it got (every task, done or not) and
// what changed its course (a message of yours, an item the agent skipped or
// added, a choice it made) had no place: the user asked "what is the status?".

const STATUS_ICON: Record<string, string> = {
  done: '✓', won: '✓', running: '▶', failed: '✗', skipped: '–', cancelled: '–',
  left: '!', pending: '○', planned: '○',
};
const STATUS_COLOR: Record<string, string> = {
  done: 'var(--ok)', won: 'var(--ok)', running: 'var(--accent)', failed: 'var(--err)',
  left: 'var(--warn)', skipped: 'var(--fg-3)', cancelled: 'var(--fg-3)',
};
const BY_LABEL: Record<string, string> = { you: 'you', agent: 'agent', harness: 'AIForge' };
const CLOSED = new Set(['done', 'won', 'skipped', 'failed', 'cancelled', 'left']);
const POLL_MS = 4000;
const DECISIONS_SHOWN = 8;

function hhmm(t: number): string {
  const d = new Date(t * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
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

function DecisionRow({ d }: Readonly<{ d: ChatDecision }>) {
  const [open, setOpen] = useState(false);
  return (
    <li className="run-ov-decision">
      <span className={`run-ov-by by-${d.by}`}>{BY_LABEL[d.by] ?? d.by}</span>
      <span style={{ flex: 1, minWidth: 0 }}>
        {d.text}
        {d.why && (
          <button type="button" className="ghost xs" style={{ padding: '0 4px', marginLeft: 4 }}
                  onClick={() => setOpen(v => !v)} title="What led to it">
            {open ? 'hide why' : 'why'}
          </button>
        )}
        {open && d.why && <div className="xs muted" style={{ marginTop: 2 }}>Your message: {d.why}</div>}
      </span>
      <span className="xs muted" style={{ flexShrink: 0 }}>{hhmm(d.t)}</span>
    </li>
  );
}

function NowLine({ running, next, busy, items }: Readonly<{
  running?: SubtaskItem; next?: SubtaskItem; busy: boolean; items: SubtaskItem[];
}>) {
  if (running) {
    return (
      <div><span style={{ color: 'var(--accent)' }}>▶</span> {running.title || running.goal || running.slug}
        <span className="xs muted"> · task {items.indexOf(running) + 1} of {items.length}</span></div>
    );
  }
  if (next) return <div className="muted">Next up: {next.title || next.goal || next.slug}</div>;
  return <div className="xs muted">{busy ? 'Starting…' : 'Nothing running.'}</div>;
}

export function RunOverview({ sessionId, busy, awaiting, subtasks, steps, phase, onClose }: Readonly<{
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
}>) {
  const [ov, setOv] = useState<ChatOverview | null>(null);
  const [allDecisions, setAllDecisions] = useState(false);
  // null: open when no goal is open (the finished work is then the news).
  const [showClosedGoals, setShowClosedGoals] = useState<boolean | null>(null);

  // Another chat: nothing of the last one's stays on screen.
  useEffect(() => { setOv(null); setAllDecisions(false); setShowClosedGoals(null); }, [sessionId]);

  useEffect(() => {
    let live = true;
    const load = () => chatOverview(sessionId)
      .then(r => { if (live) setOv(r); })
      .catch(() => { /* the panel keeps what it had */ });
    load();
    // Polled while the run goes on; once more when it ends (busy flips).
    const h = busy ? setInterval(load, POLL_MS) : undefined;
    return () => { live = false; if (h) clearInterval(h); };
  }, [sessionId, busy]);

  const items = subtasks ?? [];
  const running = items.find(s => s.status === 'running');
  // No item marked running: the next open one is what comes up, not "now".
  const next = running || !busy ? undefined
    : items.find(s => !CLOSED.has(s.status) && s.status !== 'planned');
  const closed = items.filter(s => CLOSED.has(s.status) && s.status !== 'left').length;
  const pct = items.length ? Math.round((closed / items.length) * 100) : 0;
  const goals = ov?.goals ?? [];
  const openGoals = goals.filter(g => g.status === 'open');
  const closedGoals = goals.filter(g => g.status !== 'open');
  const closedShown = showClosedGoals ?? openGoals.length === 0;
  const decisions = [...(ov?.decisions ?? [])].reverse();
  const shownDecisions = allDecisions ? decisions : decisions.slice(0, DECISIONS_SHOWN);
  const action = busy ? lastAction(steps) : '';

  let state = 'Idle';
  if (awaiting) state = 'Waiting for you';
  else if (busy) state = 'Running';

  return (
    <aside className="run-overview" aria-label="Run overview">
      <div className="run-ov-head">
        <strong>Run overview</strong>
        <span className={`run-ov-state ${busy && !awaiting ? 'on' : ''}`}>{state}</span>
        <button type="button" className="ghost xs" onClick={onClose} aria-label="Hide the run overview"
                title="Hide the run overview" style={{ marginLeft: 'auto', padding: '0 6px' }}>✕</button>
      </div>

      <div className="run-ov-body">
        <Section title="Goal">
          {ov?.asked && <div className="run-ov-ask">❓ Asked you: {ov.asked}</div>}
          {ov?.current && busy && <div className="run-ov-current">{ov.current}</div>}
          {openGoals.length > 0 ? (
            <ul className="run-ov-list">
              {openGoals.map(g => (
                <li key={g.id}>
                  <span style={{ color: ov?.now.includes(g.id) ? 'var(--accent)' : 'var(--fg-3)' }}>
                    {ov?.now.includes(g.id) ? '▶' : '○'}
                  </span>
                  <span>{g.text}</span>
                </li>
              ))}
            </ul>
          ) : (!ov?.current && <div className="xs muted">No open goal{closedGoals.length ? '' : ' — your next message sets one'}.</div>)}
          {closedGoals.length > 0 && (
            <>
              <button type="button" className="ghost xs" style={{ padding: '0 4px' }}
                      onClick={() => setShowClosedGoals(!closedShown)}>
                {closedShown ? '▾' : '▸'} {closedGoals.length} finished or dropped
              </button>
              {closedShown && (
                <ul className="run-ov-list muted">
                  {closedGoals.map(g => (
                    <li key={g.id}>
                      <span>{g.status === 'done' ? '✓' : '✕'}</span>
                      <span style={{ textDecoration: g.status === 'dropped' ? 'line-through' : undefined }}>{g.text}</span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </Section>

        <Section title="Now">
          <NowLine running={running} next={next} busy={busy} items={items} />
          {action && <div className="xs muted" style={{ marginTop: 2 }}>Last step: {action}</div>}
          {busy && phase && <div className="xs" style={{ marginTop: 2, color: 'var(--warn)' }}>⏳ {phase}</div>}
        </Section>

        <Section title="Plan" extra={items.length > 0 && <span className="xs muted">{closed}/{items.length} · {pct}%</span>}>
          {items.length === 0 ? (
            <div className="xs muted">No task list for this turn.</div>
          ) : (
            <>
              <div className="run-ov-bar"><span style={{ width: `${pct}%` }} /></div>
              <ol className="run-ov-plan">
                {items.map(s => (
                  <li key={s.slug} className={s.status === 'running' ? 'is-running' : ''}>
                    <span style={{ color: STATUS_COLOR[s.status] ?? 'var(--fg-3)', width: 14, flexShrink: 0 }}>
                      {STATUS_ICON[s.status] ?? '○'}
                    </span>
                    <span style={{ flex: 1, minWidth: 0,
                                   textDecoration: s.status === 'skipped' ? 'line-through' : undefined }}>
                      {s.title || s.goal || s.slug}
                      {s.self_added && <span className="run-ov-tag" title="The agent added this itself — nobody asked for it">agent added</span>}
                      {s.status === 'left' && <span className="run-ov-tag warn">not done</span>}
                    </span>
                  </li>
                ))}
              </ol>
            </>
          )}
        </Section>

        <Section title="Decisions & changes"
                 extra={decisions.length > DECISIONS_SHOWN && (
                   <button type="button" className="ghost xs" style={{ padding: '0 4px' }}
                           onClick={() => setAllDecisions(v => !v)}>
                     {allDecisions ? 'fewer' : `all ${decisions.length}`}
                   </button>
                 )}>
          {decisions.length === 0 ? (
            <div className="xs muted">None yet. A message that changes the plan, a task the agent skips or adds, and the choices it reports show here.</div>
          ) : (
            <ul className="run-ov-decisions">
              {/* index in the key: lines written together share t and may share text */}
              {shownDecisions.map((d, i) => <DecisionRow key={`${d.t}:${d.kind}:${d.text}:${i}`} d={d} />)}
            </ul>
          )}
        </Section>
      </div>
    </aside>
  );
}
