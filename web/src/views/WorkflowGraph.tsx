// Workflow DAG view — reads /api/workflow/topology and renders an SVG
// graph. KISS: linear left-to-right layout, dotted feedback edge,
// per-node status colour. Optional ?ticket=X URL param overlays
// per-node last-event status.
import { useEffect, useState } from 'react';
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
type Topology = { nodes: Node[]; edges: Edge[]; ticket?: string };

const STATUS_COLOR: Record<string, string> = {
  idle:        '#3b3b48',
  active:      '#2a6cdf',
  ok:          '#2faa66',
  done:        '#2faa66',
  failed:      '#d44',
  blocked:     '#d44',
  llm_turn:    '#2a6cdf',
  edit_block:  '#2a6cdf',
  compile_ok:  '#2faa66',
};

export default function WorkflowGraph() {
  const [params] = useSearchParams();
  const ticket = params.get('ticket') || undefined;
  const [topo, setTopo] = useState<Topology | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    // SSE live refresh — server pushes a fresh snapshot every 3s
    // (per-ticket overlay reflects newest ticket_event status).
    // Falls back to one-shot fetch on EventSource error.
    const url = `/api/workflow/stream${ticket ? `?ticket=${ticket}` : ''}`;
    let es: EventSource | null = null;
    try {
      es = new EventSource(url);
      es.onmessage = (ev) => {
        try { setTopo(JSON.parse(ev.data)); }
        catch (e) { setErr(String(e)); }
      };
      es.onerror = () => {
        es?.close();
        const fallback = `/api/workflow/topology${ticket ? `?ticket=${ticket}` : ''}`;
        fetch(fallback).then(r => r.json()).then(setTopo)
          .catch(e => setErr(String(e)));
      };
    } catch (e) {
      const fallback = `/api/workflow/topology${ticket ? `?ticket=${ticket}` : ''}`;
      fetch(fallback).then(r => r.json()).then(setTopo)
        .catch(e2 => setErr(String(e2)));
    }
    return () => { es?.close(); };
  }, [ticket]);

  if (err) return <div className="p-4 text-red-400">Topology error: {err}</div>;
  if (!topo) return <div className="p-4 text-slate-400">Loading topology…</div>;

  // Layout: column index = topological depth (architect=0, planner=1, …).
  // Compute in linear pass over edges.
  const depthMap: Record<string, number> = {};
  topo.nodes.forEach(n => { depthMap[n.id] = 0; });
  topo.edges.forEach(e => {
    if (e.from === e.to) return;
    depthMap[e.to] = Math.max(depthMap[e.to] ?? 0, (depthMap[e.from] ?? 0) + 1);
  });
  // Group by depth.
  const depths: Node[][] = [];
  topo.nodes.forEach(n => {
    const d = depthMap[n.id] ?? 0;
    (depths[d] ||= []).push(n);
  });

  const W = 1200, NODE_W = 140, NODE_H = 70, COL_GAP = 40;
  const colCount = depths.length;
  const colW = (W - COL_GAP) / colCount;
  const positions: Record<string, { x: number; y: number }> = {};
  depths.forEach((col, di) => {
    col.forEach((n, ni) => {
      positions[n.id] = {
        x: di * colW + COL_GAP,
        y: 60 + ni * (NODE_H + 30),
      };
    });
  });
  const H = Math.max(
    260,
    60 + Math.max(...depths.map(c => c.length)) * (NODE_H + 30) + 60,
  );

  return (
    <div className="p-4">
      <div className="mb-3 flex items-center gap-3 text-sm text-slate-400">
        <h2 className="text-lg text-slate-200">Workflow</h2>
        {ticket && <span className="px-2 py-0.5 rounded bg-slate-700">ticket: {ticket}</span>}
        <span className="text-xs">nodes: {topo.nodes.length} · edges: {topo.edges.length}</span>
      </div>

      <svg width={W} height={H} className="bg-slate-900 rounded">
        {/* Edges */}
        {topo.edges.map((e, i) => {
          const a = positions[e.from], b = positions[e.to];
          if (!a || !b) return null;
          const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
          const x2 = b.x,         y2 = b.y + NODE_H / 2;
          const isFeedback = depthMap[e.to] <= depthMap[e.from];
          const stroke = isFeedback ? '#888' : '#5fb';
          const dasharray = isFeedback ? '6,4' : undefined;
          // Curve the feedback edge so it's visually distinct.
          const path = isFeedback
            ? `M ${x1} ${y1} C ${x1 + 30} ${y1 - 40} ${x2 - 30} ${y2 - 40} ${x2} ${y2}`
            : `M ${x1} ${y1} L ${x2} ${y2}`;
          return (
            <g key={i}>
              <path d={path} stroke={stroke} strokeWidth={1.5}
                    strokeDasharray={dasharray} fill="none"
                    markerEnd="url(#arrow)" />
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
        </defs>

        {/* Nodes */}
        {topo.nodes.map(n => {
          const p = positions[n.id]; if (!p) return null;
          const colour = STATUS_COLOR[n.status || 'idle'] || '#3b3b48';
          return (
            <g key={n.id} transform={`translate(${p.x},${p.y})`}>
              <rect width={NODE_W} height={NODE_H} rx={8}
                    fill={colour} stroke="#222" strokeWidth={1} />
              <text x={NODE_W / 2} y={28} textAnchor="middle"
                    className="fill-slate-100" style={{ fontSize: 14, fontWeight: 600 }}>
                {n.label}
              </text>
              <text x={NODE_W / 2} y={48} textAnchor="middle"
                    className="fill-slate-300" style={{ fontSize: 10 }}>
                {n.type} · {n.tools.length} tools
              </text>
              {n.last_event_at && (
                <text x={NODE_W / 2} y={62} textAnchor="middle"
                      className="fill-slate-400" style={{ fontSize: 9 }}>
                  {new Date(n.last_event_at).toLocaleTimeString()}
                </text>
              )}
            </g>
          );
        })}
      </svg>

      <div className="mt-3 text-xs text-slate-500">
        Solid green = forward edge · Dotted grey = feedback / loop edge.
      </div>
    </div>
  );
}
