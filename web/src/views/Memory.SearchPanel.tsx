import { useState, useCallback } from 'react';
import { toast } from 'sonner';
import { api } from '../api';
import { SearchGroups } from './Memory.types';
import { SEARCH_GROUPS } from './Memory.helpers';
import { HitCard } from './Memory.HitCard';

export function SearchPanel() {
  const [q, setQ] = useState('');
  const [groups, setGroups] = useState<SearchGroups | null>(null);
  const [busy, setBusy] = useState(false);

  const run = useCallback(async () => {
    const query = q.trim();
    if (query.length < 2) return;
    setBusy(true);
    try {
      const res = await api.memorySearch(query, 'planner', 12);
      setGroups(res.groups);
    } catch (e: any) {
      toast.error('Search failed: ' + (e?.message || e));
    } finally {
      setBusy(false);
    }
  }, [q]);

  const total = groups
    ? groups.vector.length + groups.md.length + groups.other.length
    : 0;

  return (
    <div className="card">
      <div className="card-header">
        <h2>Search memory</h2>
        <span className="muted small">
          Same hybrid recall the agents use — semantic nearest-neighbour
          (sqlite-vec) + keyword (BM25) + spell-correction, fused. Results are
          split by origin: vector index vs markdown files. Try a paraphrase
          (“how do we ship a release”) or an exact id.
        </span>
      </div>
      <div className="row" style={{ gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <input
          style={{ flex: 1, minWidth: 240 }}
          placeholder="search across all memory…"
          value={q}
          onChange={e => setQ(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') run(); }}
        />
        <button type="button" onClick={run} disabled={busy || q.trim().length < 2}>
          {busy ? 'Searching…' : 'Search'}
        </button>
      </div>
      {groups && total === 0 && <div className="muted small">No matches.</div>}
      {groups && total > 0 && (
        <div className="col" style={{ gap: 16 }}>
          {SEARCH_GROUPS.filter(g => groups[g.key].length > 0).map(g => (
            <div key={g.key} className="col" style={{ gap: 8 }}>
              <div className="row small muted" style={{ gap: 8, alignItems: 'baseline' }}>
                <strong style={{ fontSize: 13 }}>{g.label}</strong>
                <span>· {groups[g.key].length}</span>
                <span style={{ marginLeft: 'auto' }}>{g.hint}</span>
              </div>
              {groups[g.key].map((h, i) => <HitCard key={i} h={h} />)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
