import { useState, useEffect, useCallback } from 'react';
import { toast } from 'sonner';
import { api, MemoryOverview } from '../api';
import { Icon } from '../icons';
import { OVERVIEW_STORES } from './Memory.helpers';
import { clickable } from '../a11y';

export function OverviewPanel() {
  const [ov, setOv] = useState<MemoryOverview | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(true);   // collapsed by default

  const load = useCallback(async () => {
    setLoading(true);
    try { setOv(await api.memoryOverview()); }
    catch { /* backend may be offline */ }
    finally { setLoading(false); }
  }, []);

  // Lazy-load the overview only when the panel is first expanded — avoids the
  // Neo4j overview query (and its cost) on every Memory-page load.
  useEffect(() => { if (!collapsed && ov === null) load(); }, [collapsed, ov, load]);

  async function clearStore(key: string, label: string) {
    if (!window.confirm(
      `Delete all data in "${label}"? This cannot be undone.\n\n` +
      `Your registered sources and configuration are preserved — ` +
      `re-index to repopulate.`)) return;
    setBusy(key);
    try {
      const r = await api.memoryClearStore(key);
      if (r.ok === false) toast.error(`${label}: ${r.reason || 'clear failed'}`);
      else toast.success(`${label}: cleared ${r.deleted ?? 0}`);
      await load();
    } catch (e: any) {
      toast.error(`${label}: ${e?.message || 'clear failed'}`);
    } finally { setBusy(null); }
  }

  async function wipeAll() {
    if (!window.confirm(
      'WIPE ALL MEMORY?\n\nThis deletes every indexed memory across the graph, ' +
      'SQLite units, markdown notes and chat history. It CANNOT be undone.\n\n' +
      'Registered sources + configuration are preserved (re-index to repopulate).'
    )) return;
    if (!window.confirm('Are you absolutely sure? Last chance.')) return;
    setBusy('__all__');
    try {
      await api.memoryClearAll();
      toast.success('All memory data wiped — sources preserved');
      await load();
    } catch (e: any) {
      toast.error(`Wipe failed: ${e?.message || 'error'}`);
    } finally { setBusy(null); }
  }

  return (
    <div className="card">
      <div className="card-header">
        <h2 {...clickable(() => setCollapsed(c => !c))}
            style={{ cursor: 'pointer', userSelect: 'none' }}
            title={collapsed ? 'Expand' : 'Collapse'}>
          {collapsed ? '▸' : '▾'} Memory overview
        </h2>
        {!collapsed && (
          <div className="row tight" style={{ alignItems: 'center' }}>
            {ov && <span className="muted small">backend: <code>{ov.backend}</code></span>}
            <button type="button"
              className="danger"
              onClick={wipeAll}
              disabled={busy !== null}
              title="Delete all memory data (sources + config preserved)"
            >
              <Icon.Trash size={14} /> Wipe ALL memory
            </button>
          </div>
        )}
      </div>

      {!collapsed && loading && (
        <div className="row" style={{ gap: 8, padding: '8px 0' }}>
          {[1, 2, 3].map(i => (
            <div key={i} className="skeleton" style={{ height: 48, flex: 1, borderRadius: 8 }} />
          ))}
        </div>
      )}

      {!collapsed && !loading && !ov && (
        <div className="muted small">Could not load overview — backend may be offline.</div>
      )}

      {!collapsed && !loading && ov && (
        <div className="stack" style={{ gap: 8 }}>
          {OVERVIEW_STORES.map(store => {
            const s = ov.stores[store.key] || {};
            const unavailable = s.available === false;
            return (
              <div key={store.key} style={{ borderBottom: '1px solid var(--border-0)' }}>
                <div
                  className="row"
                  style={{
                    justifyContent: 'space-between', alignItems: 'center',
                    padding: '8px 0',
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontWeight: 500 }}>{store.label}</div>
                    <div className="muted small" style={{ marginTop: 2 }}>
                      {unavailable
                        ? <span className="muted">unavailable: {s.reason || 'not configured'}</span>
                        : store.summary(s)}
                    </div>
                    <div className="muted xs" style={{ marginTop: 2 }}>{store.hint}</div>
                  </div>
                  <div className="row tight" style={{ alignItems: 'center', flexShrink: 0 }}>
                    <button type="button"
                      className="ghost danger"
                      onClick={() => clearStore(store.key, store.label)}
                      disabled={busy !== null || unavailable}
                      title={unavailable ? 'store unavailable' : `Empty ${store.label}`}
                    >
                      {busy === store.key ? 'Clearing…' : <><Icon.Trash size={13} /> Empty this</>}
                    </button>
                  </div>
                </div>
              </div>
            );
          })}

          {/* Sources — VIEW ONLY (registrations are config, never cleared). */}
          {ov.stores.sources && (
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', padding: '8px 0' }}>
              <div>
                <div style={{ fontWeight: 500 }}>Registered sources</div>
                <div className="muted small" style={{ marginTop: 2 }}>
                  {(ov.stores.sources.count ?? 0).toLocaleString()} registered
                  {Object.keys(ov.stores.sources.by_status || {}).length > 0 &&
                    ` — ${Object.entries(ov.stores.sources.by_status || {})
                      .map(([k, v]) => `${k} ${v}`).join(', ')}`}
                </div>
                <div className="muted xs" style={{ marginTop: 2 }}>
                  preserved across clears — re-index to repopulate
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
