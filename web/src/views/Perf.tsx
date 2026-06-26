// Per-step perf snapshot — reads /api/runtime/perf and renders rows
// grouped by event family (Search / Tool / LLM / File / Edit cycle).
// KISS: pure SVG/HTML, no chart library. Auto-refresh every 5s.
//
// Each row shows count · avg · max · total. A family header bar visualises
// the family's total wall_ms relative to the heaviest family in view, so
// you can spot which step type is dominating runtime at a glance.
import { useEffect, useMemo, useState } from 'react';

type Row = {
  event: string;
  name: string;
  count: number;
  total_ms: number;
  max_ms: number;
  extra?: any[];
};

type FamilyKey = 'Search' | 'Tool' | 'LLM' | 'File' | 'Edit cycle' | 'Other';
const FAMILY_KEYS: FamilyKey[] =
  ['Search', 'Tool', 'LLM', 'File', 'Edit cycle', 'Other'];

const FAMILY_COLOR: Record<FamilyKey, string> = {
  'Search':     '#5b8def',
  'Tool':       '#7a5fb7',
  'LLM':        '#d44a76',
  'File':       '#2faa66',
  'Edit cycle': '#dd9b3c',
  'Other':      '#888',
};

function familyOf(event: string): FamilyKey {
  // The perf recorder writes the family label verbatim into `event`
  // ("LLM" / "Tool" / "Search" / "File" / "Edit cycle"). Match those first.
  if ((FAMILY_KEYS as string[]).includes(event)) return event as FamilyKey;
  // Legacy GA-hook event names (pre_/post_ phases).
  if (event === 'post_search' || event === 'pre_search')           return 'Search';
  if (event === 'post_llm'    || event === 'pre_llm')              return 'LLM';
  if (event.startsWith('post_file') || event.startsWith('pre_file')) return 'File';
  if (event === 'post_edit'   || event === 'post_compile' ||
      event === 'post_test'   || event === 'pre_commit')           return 'Edit cycle';
  if (event === 'post_tool'   || event === 'pre_tool')             return 'Tool';
  return 'Other';
}

function fmt(n: number): string {
  if (!Number.isFinite(n)) return '—';
  if (n >= 10000) return (n / 1000).toFixed(1) + 's';
  if (n >= 1000)  return (n / 1000).toFixed(2) + 's';
  return Math.round(n) + 'ms';
}

export default function Perf() {
  const [rows, setRows]   = useState<Row[]>([]);
  const [err,  setErr]    = useState<string | null>(null);
  const [reset, setReset] = useState(false);
  const [active, setActive] = useState<Set<FamilyKey>>(new Set(FAMILY_KEYS));
  const [collapsed, setCollapsed] = useState<Set<FamilyKey>>(new Set());

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

  // Bucketize rows by family.
  const families = useMemo(() => {
    const out: Record<FamilyKey, { rows: Row[]; total: number; count: number; max: number }> =
      Object.fromEntries(FAMILY_KEYS.map(k => [k, { rows: [], total: 0, count: 0, max: 0 }])) as any;
    rows.forEach(r => {
      const fam = familyOf(r.event);
      out[fam].rows.push(r);
      out[fam].total += r.total_ms;
      out[fam].count += r.count;
      out[fam].max    = Math.max(out[fam].max, r.max_ms);
    });
    // Sort each family by total_ms desc.
    FAMILY_KEYS.forEach(k => out[k].rows.sort((a, b) => b.total_ms - a.total_ms));
    return out;
  }, [rows]);

  const grandTotal = useMemo(
    () => FAMILY_KEYS.reduce((a, k) => a + families[k].total, 0),
    [families],
  );
  const heaviestFamily = useMemo(
    () => Math.max(1, ...FAMILY_KEYS.map(k => families[k].total)),
    [families],
  );

  function toggleFamily(f: FamilyKey) {
    const next = new Set(active);
    if (next.has(f)) next.delete(f); else next.add(f);
    if (next.size === 0) return; // never empty
    setActive(next);
  }
  function toggleCollapse(f: FamilyKey) {
    const next = new Set(collapsed);
    if (next.has(f)) next.delete(f); else next.add(f);
    setCollapsed(next);
  }

  if (err) return <div className="p-4 text-red-400">Perf error: {err}</div>;

  const visibleFamilies = FAMILY_KEYS.filter(
    f => active.has(f) && families[f].rows.length > 0,
  );
  const totalRows = rows.length;

  return (
    <div className="p-4">
      <div className="mb-3 flex items-center gap-3 text-sm text-slate-400 flex-wrap">
        <h2 className="text-lg text-slate-200">Per-step performance</h2>
        <span className="text-xs">
          {totalRows} step buckets · grand total {fmt(grandTotal)} · auto-refresh 5s
        </span>
        <button
          onClick={() => load(true)}
          className="ml-auto px-2 py-0.5 rounded bg-slate-700 text-xs hover:bg-slate-600 text-slate-200">
          Reset aggregator
        </button>
      </div>

      {/* Family filter chips */}
      <div className="mb-3 flex items-center gap-2 flex-wrap">
        {FAMILY_KEYS.map(f => {
          const on = active.has(f);
          const fam = families[f];
          return (
            <button
              key={f}
              onClick={() => toggleFamily(f)}
              className="text-xs rounded px-2 py-1 border"
              style={{
                background:  on ? FAMILY_COLOR[f] : 'transparent',
                color:       on ? '#0b1220' : '#94a3b8',
                borderColor: on ? FAMILY_COLOR[f] : '#334155',
                fontWeight:  on ? 600 : 400,
                opacity:     fam.rows.length === 0 ? 0.4 : 1,
              }}
              title={`${fam.rows.length} buckets · ${fmt(fam.total)} total`}>
              {f}
              <span style={{ marginLeft: 6, opacity: 0.85 }}>
                {fam.rows.length}
              </span>
            </button>
          );
        })}
      </div>

      {totalRows === 0 ? (
        <div className="rounded bg-slate-900 border border-slate-800 p-6 text-slate-400 text-sm">
          <div className="text-slate-300 mb-1">No steps recorded yet.</div>
          <div className="text-xs">
            Hint: fire a chat at <code className="text-slate-300">/chat</code> or
            run a doer ticket from <code className="text-slate-300">/tickets</code> to
            populate this view. Toggle ndjson logging via
            {' '}<code className="text-slate-300">AIFORGE_PERF_NDJSON=0</code>.
          </div>
        </div>
      ) : visibleFamilies.length === 0 ? (
        <div className="text-slate-500 text-sm">
          All families filtered out. Re-enable a chip above.
        </div>
      ) : (
        <div className="space-y-3">
          {visibleFamilies.map(f => {
            const fam = families[f];
            const isCollapsed = collapsed.has(f);
            const familyBarPct = (fam.total / heaviestFamily) * 100;
            return (
              <div key={f}
                   className="rounded bg-slate-900 border border-slate-800 overflow-hidden">
                {/* Family header */}
                <button
                  onClick={() => toggleCollapse(f)}
                  className="w-full text-left px-3 py-2 flex items-center gap-3 hover:bg-slate-800/50">
                  <span className="text-slate-200 text-sm font-semibold"
                        style={{ minWidth: 90 }}>
                    {isCollapsed ? '▸' : '▾'} {f}
                  </span>
                  <span className="text-xs text-slate-400" style={{ minWidth: 240 }}>
                    {fam.rows.length} bucket{fam.rows.length === 1 ? '' : 's'} ·
                    {' '}{fam.count.toLocaleString()}× ·
                    total {fmt(fam.total)} · max {fmt(fam.max)}
                  </span>
                  {/* Family-total bar */}
                  <div className="flex-1 h-2 bg-slate-800 rounded overflow-hidden">
                    <div className="h-full"
                         style={{
                           width: `${familyBarPct}%`,
                           background: FAMILY_COLOR[f],
                         }} />
                  </div>
                  <span className="text-xs text-slate-400 tabular-nums"
                        style={{ minWidth: 56, textAlign: 'right' }}>
                    {grandTotal > 0
                      ? `${((fam.total / grandTotal) * 100).toFixed(0)}%`
                      : '—'}
                  </span>
                </button>

                {/* Rows */}
                {!isCollapsed && (
                  <div className="border-t border-slate-800">
                    {fam.rows.map(r => {
                      const avg = r.count > 0 ? r.total_ms / r.count : 0;
                      const widthPct = fam.total > 0
                        ? (r.total_ms / fam.total) * 100 : 0;
                      return (
                        <div key={`${r.event}:${r.name}`}
                             className="px-3 py-1.5 flex items-center gap-3 border-b border-slate-800/50 last:border-b-0 hover:bg-slate-800/30">
                          <span className="text-xs text-slate-500"
                                style={{ minWidth: 110 }}>
                            {r.event}
                          </span>
                          <span className="text-xs text-slate-200 truncate"
                                style={{ minWidth: 200, maxWidth: 360 }}
                                title={r.name}>
                            {r.name}
                          </span>
                          <div className="flex-1 h-1.5 bg-slate-800 rounded overflow-hidden">
                            <div className="h-full"
                                 style={{
                                   width: `${Math.max(2, widthPct)}%`,
                                   background: FAMILY_COLOR[f],
                                   opacity: 0.7,
                                 }} />
                          </div>
                          <span className="text-xs text-slate-400 tabular-nums"
                                style={{ minWidth: 60, textAlign: 'right' }}>
                            {r.count.toLocaleString()}×
                          </span>
                          <span className="text-xs text-slate-300 tabular-nums"
                                style={{ minWidth: 70, textAlign: 'right' }}
                                title="average">
                            avg {fmt(avg)}
                          </span>
                          <span className="text-xs text-slate-300 tabular-nums"
                                style={{ minWidth: 70, textAlign: 'right' }}
                                title="max">
                            max {fmt(r.max_ms)}
                          </span>
                          <span className="text-xs text-slate-100 tabular-nums font-semibold"
                                style={{ minWidth: 80, textAlign: 'right' }}
                                title="total wall ms">
                            {fmt(r.total_ms)}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="mt-3 text-xs text-slate-500">
        Source: <code>~/.aiforge/perf.ndjson</code> + in-memory aggregator.
        {reset && <span className="ml-2 text-amber-400">aggregator was just reset.</span>}
      </div>
    </div>
  );
}
