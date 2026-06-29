// Workflow DAG view — reads /api/workflow/topology and renders an SVG
// graph. KISS: depth-based left-to-right layout, dotted feedback edge,
// per-node status colour. Optional ?ticket=X URL param overlays
// per-node last-event status. Includes a recent-ticket dropdown so the
// operator can swap overlay without URL editing.
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

type Node = {
  id: string;
  label: string;
  type: string;
  tools: string[];
  status?: string;
  last_event_at?: string | null;
};
type Edge = { from: string; to: string; label: string };
type Topology = { nodes: Node[]; edges: Edge[]; ticket?: string | null };

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
    try {
      es = new EventSource(url);
      es.onmessage = (ev) => {
        try { if (!cancelled) setTopo(JSON.parse(ev.data)); }
        catch (e) { setErr(String(e)); }
      };
      es.onerror = () => {
        es?.close();
        const fallback = `/api/workflow/topology${qs}`;
        fetch(fallback).then(r => r.json()).then(d => !cancelled && setTopo(d))
          .catch(e => !cancelled && setErr(String(e)));
      };
    } catch (e) {
      const fallback = `/api/workflow/topology${qs}`;
      fetch(fallback).then(r => r.json()).then(d => !cancelled && setTopo(d))
        .catch(e2 => !cancelled && setErr(String(e2)));
    }
    return () => { cancelled = true; es?.close(); };
  }, [ticket]);

  function chooseTicket(v: string) {
    const next = new URLSearchParams(params);
    if (v) next.set('ticket', v); else next.delete('ticket');
    setParams(next, { replace: true });
  }

  // Layout — column index = topological depth.
  const layout = useMemo(() => {
    if (!topo) return null;
    const depthMap: Record<string, number> = {};
    topo.nodes.forEach(n => { depthMap[n.id] = 0; });
    // Two passes — handles unsorted edge list better than one.
    for (let i = 0; i < 2; i++) {
      topo.edges.forEach(e => {
        if (e.from === e.to) return;
        const next = (depthMap[e.from] ?? 0) + 1;
        if (next > (depthMap[e.to] ?? 0)) {
          // Skip the back-edge feedback→doer so depth doesn't explode.
          if ((depthMap[e.to] ?? 0) >= (depthMap[e.from] ?? 0)) return;
          depthMap[e.to] = next;
        }
      });
    }
    const depths: Node[][] = [];
    topo.nodes.forEach(n => {
      const d = depthMap[n.id] ?? 0;
      (depths[d] ||= []).push(n);
    });
    return { depthMap, depths };
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

  const { depthMap, depths } = layout;
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
    60 + Math.max(...depths.map(c => c.length)) * (NODE_H + ROW_GAP) + 60,
  );

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Workflow</h1>
          <div className="subtitle">The agent pipeline — request to result. Live status overlays per ticket.</div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          {TicketPicker}
          {ticket && <span className="chip">overlay: {ticket}</span>}
        </div>
      </div>
      <div className="small muted" style={{ marginBottom: 10 }}>
        {topo.nodes.length} nodes · {topo.edges.length} edges · live
      </div>

      <div style={{ overflowX: 'auto', border: '1px solid var(--border-1)', borderRadius: 10 }}>
      <svg width={W} height={H} style={{ background: 'var(--bg-1)', display: 'block' }}>
        {topo.edges.map((e, i) => {
          const a = positions[e.from], b = positions[e.to];
          if (!a || !b) return null;
          const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
          const x2 = b.x,         y2 = b.y + NODE_H / 2;
          const isFeedback =
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
          return (
            <g key={n.id} transform={`translate(${p.x},${p.y})`}>
              <rect width={NODE_W} height={NODE_H} rx={8}
                    fill={ts.fill} stroke={accent || ts.border}
                    strokeWidth={accent ? 2.5 : 1.5} />
              {accent && <circle cx={NODE_W - 10} cy={10} r={4} fill={accent} />}
              <text x={NODE_W / 2} y={24} textAnchor="middle"
                    style={{ fontSize: 14, fontWeight: 700, fill: '#0f172a' }}>
                {n.label}
              </text>
              <text x={NODE_W / 2} y={42} textAnchor="middle"
                    style={{ fontSize: 10, fontWeight: 600, fill: ts.border }}>
                {n.type}{n.tools.length ? ` · ${n.tools.length} tools` : ''}
              </text>
              <text x={NODE_W / 2} y={60} textAnchor="middle"
                    style={{ fontSize: 10, fill: accent || '#64748b' }}>
                {n.status || 'idle'}
              </text>
              <text x={NODE_W / 2} y={73} textAnchor="middle"
                    style={{ fontSize: 9, fill: '#94a3b8' }}>
                {relTime(n.last_event_at)}
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
        <span style={{ fontWeight: 600, marginLeft: 8 }}>Status:</span>
        <span><span style={{ color: '#3b82f6' }}>●</span> active</span>
        <span><span style={{ color: '#22c55e' }}>●</span> done</span>
        <span><span style={{ color: '#ef4444' }}>●</span> failed</span>
      </div>
    </>
  );
}
