// Live logs view — supports per-role view AND a fan-out "ALL" view that
// merges 5 SSE streams in arrival order, colour-coded by role.
//
// Knobs:
//   - role tabs (ALL / supervisor / planner / doer / feedback / learner)
//   - text filter (matches event / tool / ticket / level / body)
//   - ticket filter (only show events whose ticket == this string)
//   - pause / clear
//   - auto-scroll-to-bottom toggle
//
// Implementation notes:
//   - We open one EventSource per role for ALL view. ROLES is small (5)
//     so the connection cost is negligible vs the UX win.
//   - lines are kept capped at 1000 to bound memory.
//   - role colour is derived from ROLE_COLOR for instant scan-ability.
import { useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { logStreamURL } from '../api';
import { Icon } from '../icons';

const ROLES = [
  'intent', 'supervisor', 'enhancer', 'architect', 'planner', 'doer',
  'feedback', 'learner', 'publish', 'integration',
  'triage', 'researcher', 'refiner', 'verifier',
  'adk_runner',
] as const;
type Role = typeof ROLES[number];
type RoleSelection = 'ALL' | Role;

const ROLE_COLOR: Record<Role, string> = {
  intent:      '#22d3ee',
  supervisor:  '#a78bfa',
  enhancer:    '#2dd4bf',
  architect:   '#c084fc',
  planner:     '#60a5fa',
  doer:        '#34d399',
  feedback:    '#fbbf24',
  learner:     '#f472b6',
  publish:     '#84cc16',
  integration: '#f97316',
  triage:      '#38bdf8',
  researcher:  '#818cf8',
  refiner:     '#fb923c',
  verifier:    '#4ade80',
  adk_runner:  '#94a3b8',
};

type LogLine = {
  ts?: string;
  level?: string;
  role?: string;
  event?: string;
  tool?: string;
  ticket?: string;
  dur_ms?: number;
  tokens_out?: number;
  turn?: number;
  // unknown fields land here on parse
  [k: string]: any;
};

type Tagged = LogLine & { _role: Role; _idx: number };

const MAX_LINES = 1000;

export default function Logs() {
  const { role: urlRole } = useParams();
  const nav = useNavigate();
  const initial: RoleSelection =
    urlRole && (ROLES as readonly string[]).includes(urlRole)
      ? (urlRole as Role)
      : 'ALL';

  const [role, setRole]     = useState<RoleSelection>(initial);
  const [lines, setLines]   = useState<Tagged[]>([]);
  const [paused, setPaused] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [filter, setFilter] = useState('');
  const [ticketFilter, setTicketFilter] = useState('');
  const [connState, setConnState] =
    useState<Record<Role, 'open' | 'closed'>>(
      Object.fromEntries(ROLES.map(r => [r, 'closed'])) as any,
    );
  const [perRoleCount, setPerRoleCount] = useState<Record<Role, number>>(
    Object.fromEntries(ROLES.map(r => [r, 0])) as any,
  );

  const idxRef = useRef(0);
  const sourcesRef = useRef<Record<Role, EventSource | null>>(
    Object.fromEntries(ROLES.map(r => [r, null])) as any,
  );
  const boxRef = useRef<HTMLDivElement | null>(null);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  // Wire up SSE — fan out to all 5 roles when role==='ALL', otherwise
  // a single stream. We always keep refs cleaned up on role change so
  // we never leak EventSource handles.
  useEffect(() => {
    // Tear down any previous streams
    ROLES.forEach(r => {
      sourcesRef.current[r]?.close();
      sourcesRef.current[r] = null;
    });
    setLines([]);
    setPerRoleCount(Object.fromEntries(ROLES.map(r => [r, 0])) as any);
    setConnState(Object.fromEntries(ROLES.map(r => [r, 'closed'])) as any);
    idxRef.current = 0;

    const targets: Role[] = role === 'ALL' ? [...ROLES] : [role];

    targets.forEach(r => {
      const es = new EventSource(logStreamURL(r));
      sourcesRef.current[r] = es;
      es.onopen = () => setConnState(s => ({ ...s, [r]: 'open' }));
      es.onerror = () => setConnState(s => ({ ...s, [r]: 'closed' }));
      es.onmessage = (e) => {
        if (pausedRef.current) return;
        try {
          const j = JSON.parse(e.data) as LogLine;
          const tagged: Tagged = { ...j, _role: r, _idx: idxRef.current++ };
          setLines(prev => {
            const next = prev.length >= MAX_LINES
              ? [...prev.slice(prev.length - MAX_LINES + 1), tagged]
              : [...prev, tagged];
            return next;
          });
          setPerRoleCount(c => ({ ...c, [r]: c[r] + 1 }));
        } catch {
          /* skip malformed */
        }
      };
    });

    // Reflect role in URL so deep-links still work — but only for single-role.
    if (role !== 'ALL' && role !== urlRole) nav(`/logs/${role}`, { replace: true });
    if (role === 'ALL' && urlRole) nav('/logs', { replace: true });

    return () => {
      ROLES.forEach(r => {
        sourcesRef.current[r]?.close();
        sourcesRef.current[r] = null;
      });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [role]);

  // Auto-scroll to bottom on new lines (when toggle is on AND user hasn't
  // scrolled away). KISS: we just scroll to bottom — if you want to read
  // history, flick auto-scroll off via the toggle.
  useEffect(() => {
    if (!autoScroll || paused) return;
    const el = boxRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight });
  }, [lines, autoScroll, paused]);

  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const tk = ticketFilter.trim();
    return lines.filter(l => {
      if (tk && (l.ticket || '') !== tk) return false;
      if (!f) return true;
      // Match against a wide haystack so the filter is intuitive.
      const hay = [
        l.event, l.tool, l.ticket, l.level, l.role,
        l.ts, l.note, l.error, l.message,
      ].filter(Boolean).join(' ').toLowerCase();
      return hay.includes(f);
    });
  }, [lines, filter, ticketFilter]);

  const anyOpen = (Object.values(connState) as string[]).some(v => v === 'open');

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Live logs</h1>
          <div className="subtitle">
            {anyOpen
              ? <><span style={{ color: 'var(--ok)' }}>● connected</span> — {shown.length.toLocaleString()} / {lines.length.toLocaleString()} lines</>
              : <><span style={{ color: 'var(--warn)' }}>● reconnecting…</span></>}
          </div>
        </div>
        <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
          <div className="input-search" style={{ minWidth: 200 }}
               title="Match event / tool / ticket / level / role / body">
            <Icon.Search size={14} />
            <input placeholder="filter event/tool/ticket/level…"
                   value={filter}
                   onChange={e => setFilter(e.target.value)} />
          </div>
          <div className="input-search" style={{ minWidth: 140 }}
               title="Show only events linked to this ticket identifier">
            <Icon.Search size={14} />
            <input placeholder="ticket = ONE-…"
                   value={ticketFilter}
                   onChange={e => setTicketFilter(e.target.value)} />
          </div>
          <label className="row" style={{ gap: 4, fontSize: 12, color: 'var(--fg-2)' }}>
            <input type="checkbox" checked={autoScroll}
                   onChange={e => setAutoScroll(e.target.checked)} />
            auto-scroll
          </label>
          <button className="ghost" onClick={() => setPaused(p => !p)}>
            {paused ? 'Resume' : 'Pause'}
          </button>
          <button className="ghost" onClick={() => {
            setLines([]);
            setPerRoleCount(Object.fromEntries(ROLES.map(r => [r, 0])) as any);
          }}>Clear</button>
        </div>
      </div>

      {/* Role tabs — ALL + 5 individual roles, with per-role event counts
          and a connection dot. */}
      <div className="row" style={{ gap: 6, flexWrap: 'wrap', margin: '8px 0 12px' }}>
        <RoleTab
          label="ALL"
          active={role === 'ALL'}
          colour="#94a3b8"
          count={ROLES.reduce((a, r) => a + perRoleCount[r], 0)}
          onClick={() => setRole('ALL')}
          allOk={anyOpen}
        />
        {ROLES.map(r => (
          <RoleTab
            key={r}
            label={r}
            active={role === r || role === 'ALL'}
            highlighted={role === r}
            colour={ROLE_COLOR[r]}
            count={perRoleCount[r]}
            onClick={() => setRole(r)}
            allOk={connState[r] === 'open'}
          />
        ))}
      </div>

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <div ref={boxRef} className="event-log"
             style={{ maxHeight: 'calc(100vh - 260px)' }}>
          {shown.length === 0 && (
            <div className="empty">
              {lines.length === 0
                ? 'waiting for events… (logs only stream new lines after this view opens)'
                : 'no rows match the current filter.'}
            </div>
          )}
          {shown.map(l => (
            <div key={l._idx} className="event-row">
              <span className="ts">{(l.ts || '').slice(11, 23)}</span>
              <span className="chip sm"
                    style={{
                      background: ROLE_COLOR[l._role] + '22',
                      color: ROLE_COLOR[l._role],
                      borderColor: 'transparent',
                      fontWeight: 600,
                    }}>
                {l._role}
              </span>
              <span className={`chip sm ${levelClass(l.level)}`}>{l.level || 'info'}</span>
              {l.ticket && (
                <span className="chip sm mono"
                      onClick={() => setTicketFilter(l.ticket!)}
                      style={{ cursor: 'pointer' }}
                      title="filter by this ticket">
                  {l.ticket}
                </span>
              )}
              <span className="kind">{l.event || '(no-event)'}</span>
              {l.tool && <span className="muted">· {l.tool}</span>}
              {typeof l.dur_ms === 'number' && <span className="muted">· {l.dur_ms}ms</span>}
              {typeof l.tokens_out === 'number' && <span className="muted">· out={l.tokens_out}</span>}
              {typeof l.turn === 'number' && <span className="muted">· turn={l.turn}</span>}
              {l.note && <span className="muted">· {String(l.note).slice(0, 80)}</span>}
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

function RoleTab({
  label, active, highlighted, colour, count, onClick, allOk,
}: {
  label: string;
  active: boolean;
  highlighted?: boolean;
  colour: string;
  count: number;
  onClick: () => void;
  allOk: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className="ghost"
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 6,
        padding: '4px 10px',
        background: highlighted ? colour + '22' : (active ? 'transparent' : 'transparent'),
        color: highlighted ? colour : 'var(--fg-2)',
        border: `1px solid ${highlighted ? colour : 'var(--border-1)'}`,
        borderRadius: 6,
        fontWeight: highlighted ? 600 : 400,
      }}>
      <span style={{
        width: 6, height: 6, borderRadius: '50%',
        background: allOk ? '#22c55e' : '#64748b',
      }} />
      <span>{label}</span>
      <span style={{ fontSize: 10, color: 'var(--fg-3)' }}>
        {count.toLocaleString()}
      </span>
    </button>
  );
}

function levelClass(l?: string) {
  if (l === 'error') return 'err';
  if (l === 'warning' || l === 'warn') return 'warn';
  return '';
}
