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

  if (err) return <div className="page" style={{ color: 'var(--err)' }}>Perf error: {err}</div>;

  const visibleFamilies = FAMILY_KEYS.filter(
    f => active.has(f) && families[f].rows.length > 0,
  );
  // Only families that actually have data get a chip (drop dead Edit cycle/Other).
  const chipFamilies = FAMILY_KEYS.filter(f => families[f].rows.length > 0);
  const totalRows = rows.length;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Performance</h1>
          <div className="subtitle">
            Every LLM call and file/tool action from your Chat &amp; Doer runs is timed
            and grouped by kind of work — so you can see what dominates runtime.
          </div>
        </div>
        <button type="button" className="ghost" onClick={() => load(true)}>Clear stats</button>
      </div>

      <div className="small muted" style={{ marginBottom: 12 }}>
        {totalRows} operations tracked · {fmt(grandTotal)} total wall-clock · refreshes every 5s
      </div>

      {/* Family filter chips */}
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 14 }}>
        {chipFamilies.map(f => {
          const on = active.has(f);
          const fam = families[f];
          return (
            <button type="button" key={f} onClick={() => toggleFamily(f)}
              style={{ fontSize: 12, borderRadius: 6, padding: '3px 10px',
                       border: `1px solid ${on ? FAMILY_COLOR[f] : 'var(--border-1)'}`,
                       background: on ? FAMILY_COLOR[f] : 'transparent',
                       color: on ? '#fff' : 'var(--fg-3)', fontWeight: on ? 600 : 400, cursor: 'pointer' }}
              title={`${fam.rows.length} buckets · ${fmt(fam.total)} total`}>
              {f} <span style={{ opacity: 0.85, marginLeft: 4 }}>{fam.count.toLocaleString()}</span>
            </button>
          );
        })}
      </div>

      {(() => {
        if (totalRows === 0) return (
        <div className="card" style={{ padding: 24, color: 'var(--fg-3)' }}>
          <div style={{ color: 'var(--fg-2)', marginBottom: 4 }}>No activity recorded yet.</div>
          <div className="small">Open a chat or run a ticket and this fills in automatically.</div>
        </div>
        );
        if (visibleFamilies.length === 0) return (
        <div className="small muted">All families filtered out — re-enable a chip above.</div>
        );
        return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {visibleFamilies.map(f => {
            const fam = families[f];
            const isCollapsed = collapsed.has(f);
            const pctOfTotal = grandTotal > 0 ? (fam.total / grandTotal) * 100 : 0;
            return (
              <div key={f} className="card" style={{ padding: 0, overflow: 'hidden' }}>
                <button type="button" onClick={() => toggleCollapse(f)}
                  style={{ width: '100%', textAlign: 'left', background: 'none', border: 'none',
                           padding: '10px 14px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 12 }}>
                  <span style={{ width: 12, color: 'var(--fg-3)' }}>{isCollapsed ? '▸' : '▾'}</span>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, minWidth: 88, fontWeight: 600 }}>
                    <span style={{ width: 9, height: 9, borderRadius: 2, background: FAMILY_COLOR[f] }} />
                    {f}
                  </span>
                  <span className="small muted" style={{ minWidth: 230, fontVariantNumeric: 'tabular-nums' }}>
                    {fam.count.toLocaleString()}× · total {fmt(fam.total)} · max {fmt(fam.max)}
                  </span>
                  <span style={{ flex: 1, height: 6, background: 'var(--bg-3)', borderRadius: 3, overflow: 'hidden' }}>
                    <span style={{ display: 'block', height: '100%', width: `${pctOfTotal}%`, background: FAMILY_COLOR[f] }} />
                  </span>
                  <span className="small" style={{ minWidth: 44, textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: 'var(--fg-2)' }}>
                    {pctOfTotal.toFixed(0)}%
                  </span>
                </button>

                {!isCollapsed && (
                  <div style={{ borderTop: '1px solid var(--border-1)' }}>
                    {fam.rows.map(r => {
                      const avg = r.count > 0 ? r.total_ms / r.count : 0;
                      const widthPct = fam.total > 0 ? (r.total_ms / fam.total) * 100 : 0;
                      return (
                        <div key={`${r.event}:${r.name}`}
                             style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '6px 14px',
                                      borderBottom: '1px solid var(--border-0)', fontSize: 12 }}>
                          <span style={{ minWidth: 170, color: 'var(--fg-1)', fontFamily: 'var(--font-mono)',
                                         overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                                title={r.name}>{r.name}</span>
                          <span style={{ flex: 1, height: 5, background: 'var(--bg-3)', borderRadius: 3, overflow: 'hidden' }}>
                            <span style={{ display: 'block', height: '100%', width: `${Math.max(2, widthPct)}%`, background: FAMILY_COLOR[f], opacity: 0.75 }} />
                          </span>
                          <span className="muted" style={{ minWidth: 52, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{r.count.toLocaleString()}×</span>
                          <span style={{ minWidth: 78, textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: 'var(--fg-2)' }}>avg {fmt(avg)}</span>
                          <span style={{ minWidth: 78, textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: 'var(--fg-2)' }}>max {fmt(r.max_ms)}</span>
                          <span style={{ minWidth: 82, textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontWeight: 600, color: 'var(--fg-1)' }}>{fmt(r.total_ms)}</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
        );
      })()}

      <div className="small muted" style={{ marginTop: 14 }}>
        Source: <code>~/.aiforge/perf.ndjson</code>{reset && <span style={{ color: 'var(--warn)', marginLeft: 8 }}>· stats just cleared.</span>}
      </div>
    </>
  );
}
