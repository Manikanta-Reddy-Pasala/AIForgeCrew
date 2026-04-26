// Per-step perf snapshot — reads /api/runtime/perf and renders a
// horizontal-bar waterfall sorted by total wall-clock spent.
// KISS: pure SVG, no chart library. Auto-refresh every 5s.
import { useEffect, useState } from 'react';

type Row = {
  event: string;
  name: string;
  count: number;
  total_ms: number;
  max_ms: number;
  extra?: any[];
};

const EVENT_COLOR: Record<string, string> = {
  post_search:     '#5b8def',
  post_file_read:  '#2faa66',
  post_file_write: '#dd9b3c',
  post_tool:       '#7a5fb7',
  post_llm:        '#d44a76',
  post_compile:    '#3aa3a3',
  post_edit:       '#dd9b3c',
};

export default function Perf() {
  const [rows, setRows] = useState<Row[]>([]);
  const [err,  setErr]  = useState<string | null>(null);
  const [reset, setReset] = useState(false);

  async function load(forceReset = false) {
    try {
      const url = `/api/runtime/perf${forceReset ? '?reset=true' : ''}`;
      const r = await fetch(url);
      const d = await r.json();
      setRows(d.rows || []);
      setReset(d.reset || false);
      setErr(null);
    } catch (e: any) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(() => load(), 5000);
    return () => clearInterval(t);
  }, []);

  if (err) return <div className="p-4 text-red-400">Perf error: {err}</div>;

  const maxTotal = rows.reduce((a, r) => Math.max(a, r.total_ms), 1);
  const W = 900;
  const BAR_H = 22;
  const ROW_GAP = 6;
  const LABEL_W = 320;
  const PAD = 16;
  const H = PAD * 2 + rows.length * (BAR_H + ROW_GAP);

  return (
    <div className="p-4">
      <div className="mb-3 flex items-center gap-3 text-sm text-slate-400">
        <h2 className="text-lg text-slate-200">Per-step performance</h2>
        <span className="text-xs">{rows.length} step buckets · auto-refresh 5s</span>
        <button
          onClick={() => load(true)}
          className="ml-auto px-2 py-0.5 rounded bg-slate-700 text-xs hover:bg-slate-600">
          Reset aggregator
        </button>
      </div>

      {rows.length === 0 ? (
        <div className="text-slate-500 text-sm">
          No steps recorded yet — fire a chat or doer ticket to populate.
        </div>
      ) : (
        <svg width={W} height={H} className="bg-slate-900 rounded">
          {rows.map((r, i) => {
            const y = PAD + i * (BAR_H + ROW_GAP);
            const barW = ((W - LABEL_W - PAD * 2) * r.total_ms) / maxTotal;
            const colour = EVENT_COLOR[r.event] || '#888';
            return (
              <g key={`${r.event}:${r.name}`} transform={`translate(0,${y})`}>
                <text x={PAD} y={BAR_H * 0.7}
                      className="fill-slate-200" style={{ fontSize: 12 }}>
                  {r.event} · {r.name}
                </text>
                <rect x={LABEL_W} y={2} width={Math.max(2, barW)}
                      height={BAR_H - 4} rx={3} fill={colour} />
                <text x={LABEL_W + barW + 6} y={BAR_H * 0.7}
                      className="fill-slate-300" style={{ fontSize: 11 }}>
                  {r.total_ms} ms · {r.count}× · max {r.max_ms} ms
                </text>
              </g>
            );
          })}
        </svg>
      )}

      <div className="mt-3 text-xs text-slate-500">
        Source: <code>~/.aiforge/perf.ndjson</code> + in-memory aggregator.
        Toggle ndjson via <code>AIFORGE_PERF_NDJSON=0</code>.
      </div>
    </div>
  );
}
