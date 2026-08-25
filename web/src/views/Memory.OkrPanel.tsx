import { useState, useEffect, useCallback } from 'react';
import { toast } from 'sonner';
import { api } from '../api';
import { OKF_TYPE_BADGE } from './Memory.helpers';
import { OkfMeta } from './Memory.OkfMeta';

export function OkrPanel() {
  const [g, setG] = useState<any | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => { api.memoryOkr().then(setG).catch(() => setG({ nodes: [] })); }, []);
  useEffect(() => { load(); }, [load]);
  if (g === null) return null;
  const nodes: any[] = g.nodes || [];
  const objectives = nodes.filter(n => n.type === 'objective');
  const krsByObj = (oid: string) => nodes.filter(n => n.type === 'key_result' && n.parent_objective === oid);
  const learnings = nodes.filter(n => n.type === 'learning');
  const active = g.active_kr;
  const setActive = async (kr: string | null) => {
    await api.memoryOkrSetActive(kr); load();
  };
  return (
    <div className="card">
      <div className="card-header">
        <h2>🎯 OKR memory <span className="muted small">goal graph · Open Knowledge Format (OKF v0.1)</span></h2>
        <div className="row" style={{ gap: 6 }}>
          {g.counts && <span className="muted xs">{Object.entries(g.counts).map(([k, v]) => `${v} ${k}`).join(' · ')}</span>}
          <button type="button" className="ghost sm" disabled={busy} onClick={async () => {
            setBusy(true);
            try { const r = await api.memoryOkrMigrate(); toast.success(`Seeded ${r.migrated} topics into the graph`); load(); }
            catch (e: any) { toast.error(e.message); } finally { setBusy(false); }
          }} title="Seed the graph from existing topic briefs">Seed from briefs</button>
          <button type="button" className="ghost sm" onClick={load}>Refresh</button>
        </div>
      </div>
      {nodes.length === 0 ? (
        <div className="muted small">No goals yet — Objectives/Key Results/Learnings are authored automatically from sessions, or seed from your topic briefs.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {objectives.map(o => (
            <div key={o.id} style={{ borderLeft: '2px solid var(--border-1)', paddingLeft: 10 }}>
              <div style={{ fontWeight: 600 }}>{OKF_TYPE_BADGE[o.type] || '📄'} {o.title}
                <span className="muted xs"> · {o.id}{o.status ? ` · ${o.status}` : ''}</span></div>
              <OkfMeta n={o} />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3, marginTop: 4 }}>
                {krsByObj(o.id).map(kr => (
                  <div key={kr.id} style={{
                    padding: '3px 8px', borderRadius: 6,
                    background: kr.id === active ? 'var(--accent-bg, #1e2a4a)' : 'var(--bg-1)',
                  }}>
                    <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                      <span style={{ flex: 1 }}>{OKF_TYPE_BADGE.key_result} {kr.title}
                        <span className="muted xs"> · {kr.id}{kr.status ? ` · ${kr.status}` : ''}</span></span>
                      {kr.id === active
                        ? <span className="chip xs">active</span>
                        : <button type="button" className="ghost sm" onClick={() => setActive(kr.id)}>set active</button>}
                    </div>
                    <OkfMeta n={kr} />
                  </div>
                ))}
                {krsByObj(o.id).length === 0 && <span className="muted xs">no key results yet</span>}
              </div>
            </div>
          ))}
          {learnings.length > 0 && (
            <details>
              <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
                🧠 Learnings <span className="muted xs">({learnings.length})</span></summary>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 4 }}>
                {learnings.map(l => (
                  <div key={l.id} style={{ padding: '3px 8px' }}>
                    <div className="row" style={{ gap: 6 }}>
                      <span className="chip xs">{Array.isArray(l.scope) ? l.scope.join(',') : (l.scope || 'global')}</span>
                      <span className="small">{OKF_TYPE_BADGE.learning} {l.title || l.preview}</span>
                    </div>
                    <OkfMeta n={l} />
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
