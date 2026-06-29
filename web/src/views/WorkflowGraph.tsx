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
const TYPE_STYLE: Record<string, { fill: string; border: string }> = {
  start:  { fill: '#11312a', border: '#34d399' },   // emerald
  agent:  { fill: '#1e293b', border: '#60a5fa' },   // blue
  gate:   { fill: '#2c2410', border: '#fbbf24' },   // amber = decision
  branch: { fill: '#0f2e2b', border: '#2dd4bf' },   // teal = parallel
  join:   { fill: '#241d33', border: '#a78bfa' },   // violet
  merge:  { fill: '#241d33', border: '#a78bfa' },
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
      className="bg-slate-800 text-slate-200 text-xs rounded px-2 py-1 border border-slate-700"
      style={{ minWidth: 220 }}
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
      <div className="p-4">
        <div className="text-red-400 text-sm mb-3">Topology error: {err}</div>
        <div className="text-slate-500 text-xs">
          /api/workflow/stream is unreachable. Check that aiforge-api is up.
        </div>
      </div>
    );
  }
  if (!topo || !layout) {
    return (
      <div className="p-4 text-slate-400 text-sm">Loading topology…</div>
    );
  }

  if (topo.nodes.length === 0) {
    return (
      <div className="p-4">
        <div className="mb-3 flex items-center gap-3 text-sm text-slate-400">
          <h2 className="text-lg text-slate-200">Workflow</h2>
          {TicketPicker}
        </div>
        <div className="text-slate-500 text-sm">
          No nodes returned by /api/workflow/topology. Has the orchestrator
          ever run? Fire a ticket via /tickets to populate.
        </div>
      </div>
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
    <div className="p-4">
      <div className="mb-3 flex items-center gap-3 text-sm text-slate-400 flex-wrap">
        <h2 className="text-lg text-slate-200">Workflow</h2>
        {TicketPicker}
        {ticket && (
          <span className="px-2 py-0.5 rounded bg-slate-700 text-slate-200 text-xs">
            overlay: {ticket}
          </span>
        )}
        <span className="text-xs ml-auto">
          nodes: {topo.nodes.length} · edges: {topo.edges.length} · live SSE
        </span>
      </div>

      <div className="overflow-x-auto">
      <svg width={W} height={H} className="bg-slate-900 rounded">
        {topo.edges.map((e, i) => {
          const a = positions[e.from], b = positions[e.to];
          if (!a || !b) return null;
          const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
          const x2 = b.x,         y2 = b.y + NODE_H / 2;
          const isFeedback =
            (depthMap[e.to] ?? 0) <= (depthMap[e.from] ?? 0);
          const stroke = isFeedback ? '#888' : '#5fb';
          const dasharray = isFeedback ? '6,4' : undefined;
          const path = isFeedback
            ? `M ${x1} ${y1} C ${x1 + 30} ${y1 + 50} ${x2 - 30} ${y2 + 50} ${x2} ${y2}`
            : `M ${x1} ${y1} L ${x2} ${y2}`;
          return (
            <g key={i}>
              <path d={path} stroke={stroke} strokeWidth={1.5}
                    strokeDasharray={dasharray} fill="none"
                    markerEnd={isFeedback ? 'url(#arrow-fb)' : 'url(#arrow)'} />
              <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 6}
                    textAnchor="middle" className="fill-slate-400"
                    style={{ fontSize: 10 }}>
                {e.label}
              </text>
            </g>
          );
        })}
        <defs>
          <marker id="arrow" markerWidth="10" markerHeight="10"
                  refX="8" refY="3" orient="auto">
            <path d="M0,0 L0,6 L9,3 z" fill="#5fb" />
          </marker>
          <marker id="arrow-fb" markerWidth="10" markerHeight="10"
                  refX="8" refY="3" orient="auto">
            <path d="M0,0 L0,6 L9,3 z" fill="#888" />
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
                    className="fill-slate-100"
                    style={{ fontSize: 14, fontWeight: 600 }}>
                {n.label}
              </text>
              <text x={NODE_W / 2} y={42} textAnchor="middle"
                    style={{ fontSize: 10, fill: ts.border }}>
                {n.type}{n.tools.length ? ` · ${n.tools.length} tools` : ''}
              </text>
              <text x={NODE_W / 2} y={60} textAnchor="middle"
                    className="fill-slate-300" style={{ fontSize: 10 }}>
                {n.status || 'idle'}
              </text>
              <text x={NODE_W / 2} y={73} textAnchor="middle"
                    className="fill-slate-500" style={{ fontSize: 9 }}>
                {relTime(n.last_event_at)}
              </text>
            </g>
          );
        })}
      </svg>
      </div>

      <div className="mt-3 text-xs text-slate-500 flex flex-wrap gap-x-4 gap-y-1">
        <span className="text-slate-400">Node type:</span>
        <span><span style={{ color: '#34d399' }}>■</span> start</span>
        <span><span style={{ color: '#60a5fa' }}>■</span> agent</span>
        <span><span style={{ color: '#fbbf24' }}>◆</span> gate (decision)</span>
        <span><span style={{ color: '#2dd4bf' }}>■</span> parallel branch</span>
        <span><span style={{ color: '#a78bfa' }}>■</span> join / merge</span>
        <span className="text-slate-400 ml-2">Status:</span>
        <span><span style={{ color: '#3b82f6' }}>●</span> active</span>
        <span><span style={{ color: '#22c55e' }}>●</span> done</span>
        <span><span style={{ color: '#ef4444' }}>●</span> failed</span>
      </div>
    </div>
  );
}
