// MCP marketplace — browse a curated catalog of MCP servers and install /
// enable / disable / test them one-click, instead of hand-editing the
// AIFORGE_MCP_ENDPOINTS env var. Installed HTTP/SSE servers are merged into
// the Planner + Doer's MCP client automatically.
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { mcpApi, McpCatalogEntry, McpServer } from '../api';

export default function Tools() {
  const [catalog, setCatalog] = useState<McpCatalogEntry[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string>('');   // id being acted on

  async function reload() {
    try {
      const [c, s] = await Promise.all([mcpApi.catalog(), mcpApi.servers()]);
      setCatalog(c.catalog || []);
      setServers(s.servers || []);
    } catch (e: any) {
      toast.error(`Couldn't load MCP marketplace: ${e.message}`);
    } finally { setLoading(false); }
  }
  useEffect(() => { reload(); }, []);

  const installedByCatalog = new Map(servers.map(s => [s.catalog_id, s]));

  async function install(entry: McpCatalogEntry) {
    let url = entry.url;
    let name: string | undefined;
    let api_key: string | undefined;
    if (!entry.url) {   // custom template — prompt for a url
      const u = window.prompt(`URL for "${entry.name}" (http/https MCP endpoint):`, '');
      if (!u) return;
      url = u.trim();
      const n = window.prompt('Display name:', entry.name);
      name = (n || entry.name).trim();
    }
    if (entry.needs_api_key) {
      const k = window.prompt(`API key for "${entry.name}" (leave blank if none):`, '');
      if (k) api_key = k.trim();
    }
    setBusy(entry.id);
    try {
      await mcpApi.install({ catalog_id: entry.id, url, name, api_key });
      toast.success(`Installed ${entry.name}`);
      reload();
    } catch (e: any) {
      toast.error(`Install failed: ${e.message}`);
    } finally { setBusy(''); }
  }

  async function toggle(s: McpServer) {
    setBusy(s.id);
    try { await mcpApi.update(s.id, { enabled: !s.enabled }); reload(); }
    catch (e: any) { toast.error(e.message); }
    finally { setBusy(''); }
  }
  async function uninstall(s: McpServer) {
    if (!window.confirm(`Uninstall ${s.name}?`)) return;
    setBusy(s.id);
    try { await mcpApi.remove(s.id); toast.success('Uninstalled'); reload(); }
    catch (e: any) { toast.error(e.message); }
    finally { setBusy(''); }
  }
  async function test(s: McpServer) {
    setBusy(s.id);
    try {
      const r = await mcpApi.test(s.id);
      if (r.ok) toast.success(`${s.name}: ${r.tools?.length ?? 0} tool(s) reachable`);
      else toast.error(`${s.name}: ${r.error || 'unreachable'}`);
    } catch (e: any) { toast.error(e.message); }
    finally { setBusy(''); }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>MCP marketplace</h1>
          <div className="subtitle">
            Install MCP servers for the Planner + Doer — one click, no env editing.
          </div>
        </div>
      </div>

      {loading ? (
        <div className="muted">Loading…</div>
      ) : (
        <>
          {servers.length > 0 && (
            <div className="card" style={{ marginBottom: 16 }}>
              <h2 style={{ margin: '0 0 10px' }}>Installed ({servers.length})</h2>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {servers.map(s => (
                  <div key={s.id} style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: 8,
                    border: '1px solid var(--border-0)', borderRadius: 8,
                  }}>
                    <span style={{ width: 8, height: 8, borderRadius: 4, flexShrink: 0,
                                   background: s.enabled ? '#22c55e' : '#94a3b8' }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontWeight: 600 }}>{s.name}
                        {s.api_key_set && <span className="muted xs"> · 🔑</span>}</div>
                      <div className="muted xs" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {s.url || '(no url)'} · {s.transport}
                      </div>
                    </div>
                    <button type="button" className="ghost sm" disabled={busy === s.id} onClick={() => test(s)}>Test</button>
                    <button type="button" className="ghost sm" disabled={busy === s.id} onClick={() => toggle(s)}>
                      {s.enabled ? 'Disable' : 'Enable'}
                    </button>
                    <button type="button" className="ghost sm danger" disabled={busy === s.id} onClick={() => uninstall(s)}>Remove</button>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="card">
            <h2 style={{ margin: '0 0 10px' }}>Catalog</h2>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(260px,1fr))', gap: 12 }}>
              {catalog.map(c => {
                const inst = installedByCatalog.get(c.id);
                return (
                  <div key={c.id} style={{
                    padding: 12, border: '1px solid var(--border-0)', borderRadius: 10,
                    display: 'flex', flexDirection: 'column', gap: 6,
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontWeight: 700 }}>{c.name}</span>
                      {c.category && <span className="muted xs">{c.category}</span>}
                    </div>
                    <div className="muted xs" style={{ flex: 1 }}>{c.description}</div>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      {(() => {
                        if (!c.installable) return (
                        <span className="muted xs">stdio — not installable yet</span>
                        );
                        if (inst && c.id !== 'custom-http') return (
                        <span className="xs" style={{ color: '#22c55e', fontWeight: 600 }}>✓ installed</span>
                        );
                        return (
                        <button type="button" className="sm" disabled={busy === c.id} onClick={() => install(c)}>
                          {c.id === 'custom-http' ? 'Add…' : 'Install'}
                        </button>
                        );
                      })()}
                      {c.homepage && (
                        <a className="xs muted" href={c.homepage} target="_blank" rel="noreferrer"
                           style={{ marginLeft: 'auto' }}>docs ↗</a>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </>
      )}
    </>
  );
}
