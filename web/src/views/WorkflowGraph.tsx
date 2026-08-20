// Workflow DAG view — reads /api/workflow/topology and renders an SVG
// graph. KISS: depth-based left-to-right layout, dotted feedback edge,
// per-node status colour. Optional ?ticket=X URL param overlays
// per-node last-event status. Includes a recent-ticket dropdown so the
// operator can swap overlay without URL editing.
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { j } from '../api/core';

type CtxSkill = { name: string; why?: string };
type CtxRule = { name: string; source?: string };
type RunContext = { skills: CtxSkill[]; rules: CtxRule[]; workflows: CtxSkill[] };
type Node = {
  id: string;
  label: string;
  type: string;
  tools: string[];
  status?: string;
  last_event_at?: string | null;
  // Workflow-transparency: the skills/rules/workflows this stage pulled into
  // its context, and how each was chosen (why = always | match).
  skills?: CtxSkill[];
  rules?: CtxRule[];
  workflows?: CtxSkill[];
};
type Edge = { from: string; to: string; label: string };
type Topology = {
  nodes: Node[]; edges: Edge[]; ticket?: string | null;
  context?: RunContext;
};

type Ticket = {
  identifier: string;
  title: string;
  status: string;
  updated_at?: string;
};

// Status palette — covers the values emitted by /api/workflow/topology
// (idle / stage_active / stage_done / failed / blocked) AND the legacy
// per-step values (active, ok, llm_turn, etc.) so older runs still
// render with the right colour instead of falling through to grey.
const STATUS_COLOR: Record<string, string> = {
  idle:         '#3b3b48',
  stage_active: '#2a6cdf',
  stage_done:   '#2faa66',
  active:       '#2a6cdf',
  ok:           '#2faa66',
  done:         '#2faa66',
  failed:       '#d44',
  blocked:      '#d44',
  llm_turn:     '#2a6cdf',
  edit_block:   '#2a6cdf',
  compile_ok:   '#2faa66',
};

// Node FILL encodes the node's TYPE (what it is); STATUS only drives the border
// + a corner dot (what's happening) — so the two read as separate channels
// instead of every box being the same dark slate.
// Light tints (the app is light-themed) with a strong type-colored border, so
// the node TYPE reads at a glance and the dark label text stays legible.
const TYPE_STYLE: Record<string, { fill: string; border: string }> = {
  start:  { fill: '#ecfdf5', border: '#10b981' },   // emerald
  agent:  { fill: '#eff6ff', border: '#3b82f6' },   // blue
  gate:   { fill: '#fffbeb', border: '#f59e0b' },   // amber = decision
  branch: { fill: '#f0fdfa', border: '#14b8a6' },   // teal = parallel
  join:   { fill: '#f5f3ff', border: '#8b5cf6' },   // violet
  merge:  { fill: '#f5f3ff', border: '#8b5cf6' },
};
const STATUS_ACCENT: Record<string, string> = {
  stage_active: '#3b82f6', active: '#3b82f6', llm_turn: '#3b82f6', edit_block: '#3b82f6',
  stage_done: '#22c55e', done: '#22c55e', ok: '#22c55e', compile_ok: '#22c55e',
  failed: '#ef4444', blocked: '#ef4444',
};

function relTime(iso?: string | null): string {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const dt = (Date.now() - t) / 1000;
  if (dt < 60)    return `${Math.max(0, Math.round(dt))}s ago`;
  if (dt < 3600)  return `${Math.round(dt / 60)}m ago`;
  if (dt < 86400) return `${Math.round(dt / 3600)}h ago`;
  return new Date(iso).toLocaleString();
}

export default function WorkflowGraph() {
  const [params, setParams] = useSearchParams();
  const ticket = params.get('ticket') || '';
  const [topo, setTopo] = useState<Topology | null>(null);
  const [err, setErr]   = useState<string | null>(null);
  const [tickets, setTickets] = useState<Ticket[]>([]);

  // Recent tickets for the overlay dropdown — fetched once.
  useEffect(() => {
    fetch('/api/tickets?limit=20')
      .then(r => r.ok ? r.json() : [])
      .then((rows: any[]) => setTickets(
        rows.map(r => ({
          identifier: r.identifier, title: r.title,
          status: r.status, updated_at: r.updated_at,
        })),
      ))
      .catch(() => { /* dropdown stays empty, not fatal */ });
  }, []);

  useEffect(() => {
    // SSE live refresh — server pushes a fresh snapshot every ~3s
    // (per-ticket overlay reflects newest ticket_event status).
    // Falls back to one-shot fetch on EventSource error so the page
    // never sits forever blank.
    setErr(null);
    const qs = ticket ? `?ticket=${encodeURIComponent(ticket)}` : '';
    const url = `/api/workflow/stream${qs}`;
    let es: EventSource | null = null;
    let cancelled = false;
    // The SSE stream's fallback. It used to be a bare fetch with no status
    // check: a 401 or a 500 returns a JSON error BODY, which parses fine and
    // became the topology — then `topo.edges.forEach` threw during render and
    // the view died, with `.catch` never firing because nothing rejected.
    // EventSource cannot send an Authorization header, so a UI opened from
    // another host (see api/core.ts) 401s here every time. Going through `j`
    // turns a non-2xx into a throw, which the error state already handles.
    const loadFallback = () => {
      j<any>(`/workflow/topology${qs}`)
        .then(d => { if (!cancelled && d && Array.isArray(d.nodes)) setTopo(d); })
        .catch(e => !cancelled && setErr(String(e)));
    };
    try {
      es = new EventSource(url);
      es.onmessage = (ev) => {
        try {
          const d = JSON.parse(ev.data);
          // Shape-check before it reaches render: everything below indexes
          // nodes/edges directly, so a payload without them is a crash, not a
          // display glitch.
          if (!cancelled && d && Array.isArray(d.nodes)) setTopo(d);
          else if (!cancelled) setErr('workflow topology: unexpected payload');
        } catch (e) { setErr(String(e)); }
      };
      es.onerror = () => {
        es?.close();
        loadFallback();
      };
    } catch (e) {
      loadFallback();
    }
    return () => { cancelled = true; es?.close(); };
  }, [ticket]);

  function chooseTicket(v: string) {
    const next = new URLSearchParams(params);
    if (v) next.set('ticket', v); else next.delete('ticket');
    setParams(next, { replace: true });
  }

  // Layout — column index = topological depth (longest path from start).
  const layout = useMemo(() => {
    if (!topo) return null;
    // 1. Find the back-edges (loops: feedback→doer, replan, gap, loop) via DFS,
    //    so they don't corrupt the depth pass. An edge to a node currently on
    //    the recursion stack is a back-edge.
    const adj: Record<string, string[]> = {};
    topo.edges.forEach(e => { if (e.from !== e.to) (adj[e.from] ||= []).push(e.to); });
    const state: Record<string, number> = {};   // 0 none · 1 on-stack · 2 done
    const back = new Set<string>();
    const dfs = (u: string) => {
      state[u] = 1;
      (adj[u] || []).forEach(v => {
        if (state[v] === 1) back.add(`${u}>${v}`);
        else if (!state[v]) dfs(v);
      });
      state[u] = 2;
    };
    topo.nodes.forEach(n => { if (!state[n.id]) dfs(n.id); });

    // 2. Longest-path depth using only forward edges (relax until stable).
    const depthMap: Record<string, number> = {};
    topo.nodes.forEach(n => { depthMap[n.id] = 0; });
    const fwd = topo.edges.filter(e => e.from !== e.to && !back.has(`${e.from}>${e.to}`));
    for (let pass = 0; pass < topo.nodes.length + 1; pass++) {
      let changed = false;
      fwd.forEach(e => {
        const nd = (depthMap[e.from] ?? 0) + 1;
        if (nd > (depthMap[e.to] ?? 0)) { depthMap[e.to] = nd; changed = true; }
      });
      if (!changed) break;
    }
    const depths: Node[][] = [];
    topo.nodes.forEach(n => {
      const d = depthMap[n.id] ?? 0;
      (depths[d] ||= []).push(n);
    });
    return { depthMap, depths, back };
  }, [topo]);

  const TicketPicker = (
    <select
      value={ticket}
      onChange={e => chooseTicket(e.target.value)}
      style={{ minWidth: 220, fontSize: 13, padding: '4px 8px' }}
      title="Overlay per-node status from this ticket"
    >
      <option value="">— no overlay —</option>
      {tickets.map(t => (
        <option key={t.identifier} value={t.identifier}>
          {t.identifier} · {t.status} · {t.title.slice(0, 48)}
        </option>
      ))}
    </select>
  );

  if (err) {
    return (
      <div className="page">
        <div style={{ color: 'var(--err)', marginBottom: 8 }}>Topology error: {err}</div>
        <div className="small muted">/api/workflow/stream is unreachable. Check that aiforge-api is up.</div>
      </div>
    );
  }
  if (!topo || !layout) {
    return <div className="page muted">Loading topology…</div>;
  }

  if (topo.nodes.length === 0) {
    return (
      <>
        <div className="page-header">
          <div><h1>Workflow</h1></div>
          {TicketPicker}
        </div>
        <div className="small muted">
          No nodes returned by /api/workflow/topology. Has the orchestrator
          ever run? Fire a ticket via /tickets to populate.
        </div>
      </>
    );
  }

  const { depthMap, depths, back } = layout;
  const NODE_W = 150, NODE_H = 78, COL_GAP = 90, ROW_GAP = 34;
  const colCount = depths.length || 1;
  // Fixed per-column stride (was cramming every column into 1200px, so 150px
  // nodes overlapped by ~90px → an illegible smear). Grow the canvas + scroll.
  const colStride = NODE_W + COL_GAP;
  const W = COL_GAP + colCount * colStride;
  const positions: Record<string, { x: number; y: number }> = {};
  depths.forEach((col, di) => {
    col.forEach((n, ni) => {
      positions[n.id] = {
        x: COL_GAP + di * colStride,
        y: 60 + ni * (NODE_H + ROW_GAP),
      };
    });
  });
  const H = Math.max(
    260,
    60 + Math.max(0, ...depths.map(c => c?.length ?? 0)) * (NODE_H + ROW_GAP) + 60,
  );

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Workflow</h1>
          <div className="subtitle">The agent pipeline — how a request becomes a result.</div>
        </div>
      </div>
      <div className="small muted" style={{ marginBottom: 6 }}>
        {(topo.nodes?.length ?? 0)} nodes · {(topo.edges?.length ?? 0)} edges · live
      </div>
      {/* Flow caption — names the stages so the layers (orchestrator, the
          parallel context fan-out, the build loop) are obvious. */}
      <div className="small" style={{ marginBottom: 10, color: 'var(--fg-2)', lineHeight: 1.5 }}>
        Triage → <b>Orchestrator</b> (Enhancer → Planner) → <b>parallel Context fan-out</b>
        {' '}(Researcher + repo-map + conventions) → Verify → <b>Build loop</b>
        {' '}(Doer → Refiner → Feedback) → Validate → Learn.
        <span className="muted"> Hover any node for what it does.</span>
      </div>

      {/* Execution modes — how a chat request is actually run, and how it
          right-sizes itself from Simple to the full Pipeline. */}
      <div className="wf-modes">
        <div className="wf-mode">
          <div className="wf-mode-h">⚡ Simple</div>
          <div className="wf-mode-b">
            One agent, full filesystem + shell tools, edits in place. Best for a
            bug fix, an edit, or a question. Analyze/explain queries stay
            read-only (no build). <b>Auto-escalates to Pipeline</b> when it detects
            a multi-file build.
          </div>
        </div>
        <div className="wf-mode">
          <div className="wf-mode-h">📋 Plan</div>
          <div className="wf-mode-b">
            Read-only. Produces a step-by-step plan and changes <b>nothing</b> until
            you press <b>Approve &amp; Execute</b>. A build request also
            auto-escalates to the Pipeline.
          </div>
        </div>
        <div className="wf-mode">
          <div className="wf-mode-h">🔀 Pipeline <span className="muted">(team)</span></div>
          <div className="wf-mode-b">
            Decompose → scaffold → <b>parallel Doers</b> (fresh context each) →
            <b> reconcile loop</b> (test → repo-map-informed fix → escalate the
            stuck residual → audit a wrong test) → integration test. Best for
            building or large multi-file tasks.
          </div>
        </div>
        <div className="wf-mode wf-mode-auto">
          <div className="wf-mode-h">↗ Auto-transition</div>
          <div className="wf-mode-b">
            You don't have to pick. Simple/Plan detect a multi-file build (a build
            verb + a project noun, or “with tests”/“endpoints”) and route it
            through the Pipeline automatically — the mode <b>right-sizes itself</b>
            to the task.
          </div>
        </div>
      </div>

      <div style={{ width: '100%', maxWidth: '100%', overflow: 'auto', maxHeight: '78vh',
                    border: '1px solid var(--border-1)', borderRadius: 10,
                    boxSizing: 'border-box', WebkitOverflowScrolling: 'touch' }}>
      <svg width={W} height={H} style={{ background: 'var(--bg-1)', display: 'block',
                                          maxWidth: 'none', flexShrink: 0 }}>
        {topo.edges.map((e, i) => {
          const a = positions[e.from], b = positions[e.to];
          if (!a || !b) return null;
          const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
          const x2 = b.x,         y2 = b.y + NODE_H / 2;
          const isFeedback =
            back.has(`${e.from}>${e.to}`) ||
            (depthMap[e.to] ?? 0) <= (depthMap[e.from] ?? 0);
          const stroke = isFeedback ? '#d4a72c' : '#94a3b8';
          const dasharray = isFeedback ? '6,4' : undefined;
          const path = isFeedback
            ? `M ${x1} ${y1} C ${x1 + 30} ${y1 + 50} ${x2 - 30} ${y2 + 50} ${x2} ${y2}`
            : `M ${x1} ${y1} L ${x2} ${y2}`;
          return (
            <g key={i}>
              <path d={path} stroke={stroke} strokeWidth={1.5}
                    strokeDasharray={dasharray} fill="none"
                    markerEnd={isFeedback ? 'url(#arrow-fb)' : 'url(#arrow)'} />
              {e.label && (
                <>
                  <rect x={(x1 + x2) / 2 - e.label.length * 3.4 - 4} y={(y1 + y2) / 2 - 17}
                        width={e.label.length * 6.8 + 8} height={14} rx={3}
                        fill="var(--bg-0)" stroke="var(--border-1)" />
                  <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 6}
                        textAnchor="middle" style={{ fontSize: 10, fill: '#475569', fontWeight: 600 }}>
                    {e.label}
                  </text>
                </>
              )}
            </g>
          );
        })}
        <defs>
          <marker id="arrow" markerWidth="10" markerHeight="10"
                  refX="8" refY="3" orient="auto">
            <path d="M0,0 L0,6 L9,3 z" fill="#94a3b8" />
          </marker>
          <marker id="arrow-fb" markerWidth="10" markerHeight="10"
                  refX="8" refY="3" orient="auto">
            <path d="M0,0 L0,6 L9,3 z" fill="#d4a72c" />
          </marker>
        </defs>

        {topo.nodes.map(n => {
          const p = positions[n.id]; if (!p) return null;
          const ts = TYPE_STYLE[n.type] || TYPE_STYLE.agent;
          const accent = STATUS_ACCENT[n.status || ''];
          const nSk = n.skills?.length || 0;
          const nRu = n.rules?.length || 0;
          const nWf = n.workflows?.length || 0;
          const ctxN = nSk + nRu + nWf;
          // Tooltip lists exactly what this stage used + how it was chosen.
          const ctxTip = ctxN
            ? '\nContext used:'
              + (n.skills || []).map(s => `\n  • skill: ${s.name}${s.why ? ` (${s.why})` : ''}`).join('')
              + (n.workflows || []).map(w => `\n  • workflow: ${w.name}${w.why ? ` (${w.why})` : ''}`).join('')
              + (n.rules || []).map(r => `\n  • rule: ${r.name}${r.source ? ` (${r.source})` : ''}`).join('')
            : '';
          return (
            <g key={n.id} transform={`translate(${p.x},${p.y})`}>
              <title>{(n as any).stage ? `${(n as any).stage} — ` : ''}{(n as any).desc || n.label}{ctxTip}</title>
              <rect width={NODE_W} height={NODE_H} rx={8}
                    fill={ts.fill} stroke={accent || ts.border}
                    strokeWidth={accent ? 2.5 : 1.5} />
              <text x={NODE_W / 2} y={28} textAnchor="middle"
                    style={{ fontSize: 14, fontWeight: 700, fill: '#0f172a' }}>
                {n.label}
              </text>
              <text x={NODE_W / 2} y={45} textAnchor="middle"
                    style={{ fontSize: 10, fontWeight: 600, fill: ts.border }}>
                {n.type}{n.tools.length ? ` · ${n.tools.length} tools` : ''}
              </text>
              {ctxN > 0 && (
                <text x={NODE_W / 2} y={60} textAnchor="middle"
                      style={{ fontSize: 9, fontWeight: 700, fill: '#7c3aed' }}>
                  🧠 {[nSk && `${nSk} skill`, nWf && `${nWf} wf`, nRu && `${nRu} rule`]
                        .filter(Boolean).join(' · ')}
                </text>
              )}
              <text x={NODE_W / 2} y={ctxN > 0 ? 71 : 66} textAnchor="middle"
                    style={{ fontSize: 9, fill: '#94a3b8' }}>
                {(n as any).stage || ''}
              </text>
            </g>
          );
        })}
      </svg>
      </div>

      <div className="small muted" style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', columnGap: 16, rowGap: 4, alignItems: 'center' }}>
        <span style={{ fontWeight: 600 }}>Type:</span>
        <span><span style={{ color: '#10b981' }}>■</span> start</span>
        <span><span style={{ color: '#3b82f6' }}>■</span> agent</span>
        <span><span style={{ color: '#f59e0b' }}>■</span> gate (decision)</span>
        <span><span style={{ color: '#14b8a6' }}>■</span> parallel branch</span>
        <span><span style={{ color: '#8b5cf6' }}>■</span> join / merge</span>
      </div>

      {/* Static guide — always visible. Explains what skills / workflows /
          rules ARE, when each is selected, how it reaches the run, and which
          agents are affected, independent of whether a ticket is selected. */}
      <KnowledgeGuide />

      {/* Context panel — what extra knowledge THIS run pulled in, and HOW each
          was chosen, so the workflow itself explains the skills/rules/workflows
          the agents used. Only shown when a ticket overlay has context. */}
      <ContextPanel ctx={topo.context} ticket={ticket} />
    </>
  );
}

function KnowledgeGuide() {
  const cards: { title: string; color: string; what: string;
                 when: string; how: string; agents: string }[] = [
    {
      title: 'Skills', color: '#3b82f6',
      what: 'Reusable SKILL.md playbooks — a named procedure the agent can follow (e.g. "add a REST endpoint", "write a migration").',
      when: 'Selected by relevance to the ticket title + body (match), or marked always-on (always). Authored/extended at runtime via the learn_skill tool.',
      how: 'The matched skill bodies are prepended to the seed prompt before the agents run.',
      agents: 'Enhancer, Planner, Doer',
    },
    {
      title: 'Workflows', color: '#14b8a6',
      what: 'Reusable WORKFLOW.md end-to-end procedures — multi-step recipes spanning several tools/files for a repeatable job.',
      when: 'Selected by relevance to the ticket title + body (match), or always-on (always). Authored at runtime via the learn_workflow tool.',
      how: 'The matched workflow bodies are prepended to the seed prompt alongside skills.',
      agents: 'Enhancer, Planner, Doer',
    },
    {
      title: 'Rules', color: '#f59e0b',
      what: 'Repo conventions & guardrails (AGENTS.md / CLAUDE.md / .cursorrules / scoped rule files) the agents must respect.',
      when: 'Matched by the repo path + the ticket scope globs — not by ticket text. The source file is shown so you know where each came from.',
      how: 'The matching rule text is injected into the run context before the build loop.',
      agents: 'Enhancer, Planner, Doer',
    },
  ];
  return (
    <div style={{ marginTop: 16, padding: 12, border: '1px solid var(--border-1)',
                  borderRadius: 10, background: 'var(--bg-1)' }}>
      <div style={{ fontWeight: 700, marginBottom: 4 }}>
        📚 How rules, skills & workflows feed the pipeline
      </div>
      <div className="small muted" style={{ marginBottom: 10 }}>
        These are the knowledge sources injected into a run. They are gathered
        at prompt-build time and prepended to the seed the agents see — so they
        shape planning and implementation without you wiring them per ticket.
        Pick a ticket above to see exactly what a given run pulled in.
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
        {cards.map(c => (
          <div key={c.title} style={{ flex: '1 1 240px', minWidth: 220,
                 border: '1px solid var(--border-1)', borderRadius: 8, padding: 10,
                 background: 'var(--bg-0)' }}>
            <div style={{ fontWeight: 700, color: c.color, marginBottom: 6 }}>
              {c.title}
            </div>
            <div className="small" style={{ marginBottom: 6, lineHeight: 1.45 }}>
              {c.what}
            </div>
            <div className="small muted" style={{ marginBottom: 4, lineHeight: 1.4 }}>
              <b>When used:</b> {c.when}
            </div>
            <div className="small muted" style={{ marginBottom: 4, lineHeight: 1.4 }}>
              <b>How injected:</b> {c.how}
            </div>
            <div className="small" style={{ lineHeight: 1.4 }}>
              <b>Agents affected:</b>{' '}
              <span style={{ color: '#7c3aed', fontWeight: 600 }}>{c.agents}</span>
            </div>
          </div>
        ))}
      </div>
      <div className="small muted" style={{ marginTop: 10, lineHeight: 1.45 }}>
        🧠 The badge on a node above counts what that stage actually used.
        Only the <b>Enhancer</b>, <b>Planner</b> and <b>Doer</b> consume this
        knowledge; gates, context-fan-out and the Validator/Learner do not.
      </div>
    </div>
  );
}

function ContextPanel({ ctx, ticket }: { ctx?: RunContext; ticket: string }) {
  const skills = ctx?.skills || [];
  const rules = ctx?.rules || [];
  const workflows = ctx?.workflows || [];
  const total = skills.length + rules.length + workflows.length;
  if (!ticket) return null;
  return (
    <div style={{ marginTop: 16, padding: 12, border: '1px solid var(--border-1)',
                  borderRadius: 10, background: 'var(--bg-1)' }}>
      <div style={{ fontWeight: 700, marginBottom: 4 }}>
        🧠 Knowledge this run used
      </div>
      <div className="small muted" style={{ marginBottom: 8 }}>
        Skills, workflows and rules the agents pulled into context for{' '}
        <b>{ticket}</b> — <i>always</i> = always-on, <i>match</i> = relevance hit,
        rules show their source file. Empty until the run reaches the orchestrator.
      </div>
      {total === 0 ? (
        <div className="small muted">No skills, workflows or rules injected yet.</div>
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18 }}>
          <CtxColumn title="Skills" color="#3b82f6"
                     items={skills.map(s => ({ name: s.name, tag: s.why }))} />
          <CtxColumn title="Workflows" color="#14b8a6"
                     items={workflows.map(w => ({ name: w.name, tag: w.why }))} />
          <CtxColumn title="Rules" color="#f59e0b"
                     items={rules.map(r => ({ name: r.name, tag: r.source ? r.source.split('/').pop() : undefined }))} />
        </div>
      )}
    </div>
  );
}

function CtxColumn({ title, color, items }:
  { title: string; color: string; items: { name: string; tag?: string }[] }) {
  return (
    <div style={{ minWidth: 160 }}>
      <div style={{ fontWeight: 600, color, marginBottom: 4 }}>
        {title} ({items.length})
      </div>
      {items.length === 0 ? (
        <div className="small muted">—</div>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 16 }}>
          {items.map((it, i) => (
            <li key={i} className="small">
              {it.name}
              {it.tag && <span className="muted"> · {it.tag}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
