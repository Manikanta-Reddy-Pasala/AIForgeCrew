import { useState, useEffect, useRef, useCallback } from 'react';
import { toast } from 'sonner';
import {
  api, MemorySource, MemoryOverview, MemoryStoreSection,
} from '../api';
import { Icon } from '../icons';


const KIND_OPTIONS = [
  { value: 'repo',  label: 'Code repo' },
  { value: 'docs',  label: 'Docs folder' },
  { value: 'url',   label: 'URL' },
  { value: 'file',  label: 'File upload' },
];

function statusClass(status: string) {
  if (status === 'indexing') return 'source-status-indexing';
  if (status === 'done')     return 'source-status-done';
  if (status === 'error')    return 'source-status-error';
  return 'source-status-idle';
}

function relativeDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const diff = Date.now() - d.getTime();
  const mins  = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days  = Math.floor(diff / 86400000);
  if (mins < 1)   return 'just now';
  if (mins < 60)  return `${mins}m ago`;
  if (hours < 24) return `${hours}h ago`;
  return `${days}d ago`;
}

function truncate(s: string, n = 50): string {
  return s.length > n ? s.slice(0, n) + '…' : s;
}

// ─── Memory overview + per-datasource clear ──────────────────────────────────

// One row per clearable datasource. `summary` turns the store's section into a
// one-line "what it has" string; a section that is `available:false` renders as
// unavailable (e.g. the graph stores when running on the SQLite backend).
const OVERVIEW_STORES: {
  key: string;
  label: string;
  hint: string;
  summary: (s: MemoryStoreSection) => string;
}[] = [
  {
    key: 'sqlite', label: 'SQLite memory',
    hint: 'embedded units (learnings / failures / notes)',
    summary: s => {
      const total = (s.total ?? 0).toLocaleString();
      const kinds = Object.entries(s.by_kind || {})
        .map(([k, v]) => `${k} ${v}`).join(', ');
      return `${total} units` + (kinds ? ` — ${kinds}` : '');
    },
  },
  {
    key: 'md_files', label: 'Markdown notes',
    hint: 'human-readable .md memories on disk',
    summary: s => `${(s.count ?? 0).toLocaleString()} files` +
      (s.bytes ? ` · ${(s.bytes / 1024).toFixed(1)} KB` : ''),
  },
  {
    key: 'chat', label: 'Chat sessions',
    hint: 'saved conversations',
    summary: s => `${(s.sessions ?? 0).toLocaleString()} sessions, ` +
      `${(s.messages ?? 0).toLocaleString()} messages`,
  },
];



function OverviewPanel() {
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
        <h2 onClick={() => setCollapsed(c => !c)}
            style={{ cursor: 'pointer', userSelect: 'none' }}
            title={collapsed ? 'Expand' : 'Collapse'}>
          {collapsed ? '▸' : '▾'} Memory overview
        </h2>
        {!collapsed && (
          <div className="row tight" style={{ alignItems: 'center' }}>
            {ov && <span className="muted small">backend: <code>{ov.backend}</code></span>}
            <button
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
                    <button
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

// ─── Sources management panel ─────────────────────────────────────────────────

function SourcesPanel() {
  const [sources, setSources]   = useState<MemorySource[]>([]);
  const [loading, setLoading]   = useState(true);
  const [kind, setKind]         = useState('repo');
  const [location, setLocation] = useState('');
  const [name, setName]         = useState('');
  const [file, setFile]         = useState<File | null>(null);
  const [adding, setAdding]     = useState(false);
  const [validating, setValidating] = useState(false);

  async function validatePath() {
    if (!location.trim()) { toast.error('Enter a path first.'); return; }
    setValidating(true);
    try {
      const r = await api.memoryValidatePath(location.trim());
      if (r.ok) toast.success(`✓ ${r.code_files} code + ${r.doc_files} doc files at ${r.resolved}`);
      else toast.error(r.message, { duration: 8000 });
    } catch (e: any) {
      toast.error(`Validate failed: ${e.message}`);
    } finally {
      setValidating(false);
    }
  }
  const fileInputRef            = useRef<HTMLInputElement>(null);
  const pollRef                 = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchSources = useCallback(async () => {
    try {
      const list = await api.memorySources();
      setSources(list);
      return list;
    } catch {
      return null;
    }
  }, []);

  // Start / stop polling based on whether any source is indexing
  const managePoll = useCallback((list: MemorySource[] | null) => {
    const anyIndexing = list?.some(s => s.status === 'indexing') ?? false;
    if (anyIndexing && !pollRef.current) {
      pollRef.current = setInterval(async () => {
        const updated = await fetchSources();
        if (updated && !updated.some(s => s.status === 'indexing')) {
          clearInterval(pollRef.current!);
          pollRef.current = null;
        }
      }, 2000);
    } else if (!anyIndexing && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, [fetchSources]);

  useEffect(() => {
    fetchSources()
      .then(list => managePoll(list))
      .finally(() => setLoading(false));
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [fetchSources, managePoll]);

  // Refresh + (re)start poll after sources mutate
  const refresh = useCallback(async () => {
    const list = await fetchSources();
    managePoll(list);
  }, [fetchSources, managePoll]);

  async function handleAdd() {
    if (kind === 'file') {
      if (!file) { toast.error('Please choose a file.'); return; }
      setAdding(true);
      try {
        await api.memorySourceUpload(file, name || undefined);
        toast.success('File source added.');
        setFile(null);
        setName('');
        if (fileInputRef.current) fileInputRef.current.value = '';
        await refresh();
      } catch (e: any) {
        toast.error(`Failed to add: ${e.message}`);
      } finally {
        setAdding(false);
      }
    } else {
      if (!location.trim()) { toast.error('Location / URL is required.'); return; }
      setAdding(true);
      try {
        await api.memorySourceCreate({ kind, location: location.trim(), name: name.trim() || undefined });
        toast.success(kind === 'repo' || kind === 'docs'
          ? 'Source added — indexing started (chunks + symbols + graph).'
          : 'Source added.');
        setLocation('');
        setName('');
        await refresh();
      } catch (e: any) {
        toast.error(`Failed to add: ${e.message}`);
      } finally {
        setAdding(false);
      }
    }
  }

  async function handleIndex(id: number) {
    try {
      await api.memorySourceIndex(id);
      toast.success('Indexing started.');
      await refresh();
    } catch (e: any) {
      toast.error(`Index failed: ${e.message}`);
    }
  }

  async function handleDelete(id: number, sourceName: string) {
    if (!window.confirm(`Delete source "${sourceName}"? This cannot be undone.`)) return;
    try {
      await api.memorySourceDelete(id);
      toast.success('Source deleted.');
      await refresh();
    } catch (e: any) {
      toast.error(`Delete failed: ${e.message}`);
    }
  }

  return (
    <div className="card">
      <div className="card-header">
        <h2>Sources</h2>
        <span className="muted small">
          {sources.length} source{sources.length !== 1 ? 's' : ''} · add markdown or code — indexed on the go (Aider RepoMap + CodeGraph for code relations)
        </span>
      </div>

      {/* ── Add source form ── */}
      <div style={{ marginBottom: 20, padding: '14px 16px', background: 'var(--bg-1)', borderRadius: 'var(--r-md)', border: '1px solid var(--border-0)' }}>
        <div style={{ marginBottom: 10, fontWeight: 600, fontSize: 'var(--fs-sm)', color: 'var(--fg-0)' }}>Add source</div>
        <div className="row" style={{ flexWrap: 'wrap', gap: 8, alignItems: 'flex-end' }}>
          <label className="field" style={{ minWidth: 130 }}>
            Kind
            <select value={kind} onChange={e => { setKind(e.target.value); setLocation(''); setFile(null); setName(''); }}>
              {KIND_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>

          {kind === 'file' ? (
            <label className="field" style={{ flex: 1, minWidth: 200 }}>
              File
              <input
                ref={fileInputRef}
                type="file"
                style={{ padding: '6px 10px' }}
                onChange={e => setFile(e.target.files?.[0] ?? null)}
              />
            </label>
          ) : (
            <label className="field" style={{ flex: 2, minWidth: 220 }}>
              {kind === 'url' ? 'URL' : 'Path'}
              <input
                type="text"
                placeholder={kind === 'url' ? 'https://…' : '/absolute/path/to/repo'}
                value={location}
                onChange={e => setLocation(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && !adding && handleAdd()}
              />
            </label>
          )}

          <label className="field" style={{ flex: 1, minWidth: 140 }}>
            Name <span style={{ fontWeight: 400, textTransform: 'none', letterSpacing: 0 }}>(optional)</span>
            <input
              type="text"
              placeholder="friendly name"
              value={name}
              onChange={e => setName(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && !adding && handleAdd()}
            />
          </label>

          {(kind === 'repo' || kind === 'docs') && (
            <button onClick={validatePath} disabled={validating} className="ghost sm"
                    title="Check the path resolves + has files BEFORE indexing"
                    style={{ alignSelf: 'flex-end', marginBottom: 1 }}>
              {validating ? 'Checking…' : 'Validate path'}
            </button>
          )}
          <button onClick={handleAdd} disabled={adding} style={{ alignSelf: 'flex-end', marginBottom: 1 }}>
            {adding ? 'Adding…' : 'Add'}
          </button>
        </div>
      </div>

      {/* ── Sources table ── */}
      {loading ? (
        <div className="stack">
          {[1, 2].map(i => <div key={i} className="skeleton" style={{ height: 40, borderRadius: 6 }} />)}
        </div>
      ) : sources.length === 0 ? (
        <div className="empty" style={{ padding: '32px 16px' }}>
          <div className="empty-icon">∅</div>
          <div style={{ color: 'var(--fg-0)', fontWeight: 500 }}>No sources yet</div>
          <div>Add a repo, docs folder, URL, or file above.</div>
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Name / Location</th>
                <th>Kind</th>
                <th>Status</th>
                <th style={{ textAlign: 'right' }}>Units</th>
                <th>Last indexed</th>
                <th style={{ textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {sources.map(s => (
                <tr key={s.id}>
                  <td>
                    <div style={{ fontWeight: 500, color: 'var(--fg-0)', fontSize: 'var(--fs-md)' }}>{s.name || '—'}</div>
                    <div className="muted xs mono" title={s.location}>{truncate(s.location, 60)}</div>
                    {s.error && (
                      <div style={{ color: 'var(--err)', fontSize: 'var(--fs-xs)', marginTop: 2 }} title={s.error}>
                        {truncate(s.error, 80)}
                      </div>
                    )}
                  </td>
                  <td>
                    <span className="chip sm">{s.kind}</span>
                  </td>
                  <td>
                    <span className={`chip sm ${statusClass(s.status)}`}>
                      {s.status === 'indexing' && (
                        <span style={{ display: 'inline-block', marginRight: 3 }}>⟳</span>
                      )}
                      {s.status}
                    </span>
                  </td>
                  <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-sm)' }}>
                    {s.units ?? 0}
                  </td>
                  <td className="muted small">{relativeDate(s.last_indexed)}</td>
                  <td>
                    <div className="row tight" style={{ justifyContent: 'flex-end' }}>
                      <button
                        className="ghost sm"
                        disabled={s.status === 'indexing'}
                        onClick={() => handleIndex(s.id)}
                        title="Re-index this source"
                      >
                        Index
                      </button>
                      <button
                        className="ghost sm danger"
                        onClick={() => handleDelete(s.id, s.name || s.location)}
                        title="Delete this source"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Main Memory page ─────────────────────────────────────────────────────────

// ── memory files → user-facing CATEGORIES (Tasks / Solutions / Workflows /
// Commands / Topics), derived from kind + name + tags. One place, so the flat
// "compacted-*" dump becomes a browsable, grouped library.
const CATEGORY_ORDER = ['Workflows', 'Commands', 'Solutions', 'Tasks', 'Topics'] as const;
type Category = typeof CATEGORY_ORDER[number];

function categoryOf(f: any): Category {
  const kind = String(f.kind || '').toLowerCase();
  const name = String(f.name || '').toLowerCase();
  const tags: string[] = (f.tags || []).map((t: string) => String(t).toLowerCase());
  const has = (...xs: string[]) => xs.some(x => kind === x || tags.includes(x));
  if (has('workflow') || name.startsWith('compacted-session-') || tags.includes('workflow')) return 'Workflows';
  if (has('command') || tags.includes('command') || tags.includes('commands')) return 'Commands';
  if (has('decision', 'gotcha', 'bug', 'solution', 'fix', 'feedback', 'learning', 'project_learning')) return 'Solutions';
  if (has('task', 'session', 'project') || /^compacted-(jira|clr|rsp|\d)/.test(name)) return 'Tasks';
  return 'Topics';
}

// "compacted-sync-retry-policy" → "sync retry policy"; keeps a real title as-is.
function cleanTitle(f: any): string {
  const t = String(f.title || f.name || '').replace(/\.md$/, '');
  return t.replace(/^compacted-/, '').replace(/-/g, ' ').trim() || t;
}

const CAT_ICON: Record<Category, string> = {
  Workflows: '🔧', Commands: '⌨️', Solutions: '💡', Tasks: '📋', Topics: '🧭',
};

// ── OKR-DAG: the goal-oriented memory (objectives → key results → learnings)
// OKF (Open Knowledge Format) meta for one concept node: a type badge, the
// one-line description, tag chips, and link count (linked_krs = graph edges).
function OkfMeta({ n }: { n: any }) {
  const tags: string[] = Array.isArray(n.tags) ? n.tags : [];
  const links: string[] = Array.isArray(n.linked_krs) ? n.linked_krs : [];
  return (
    <>
      {n.description && <div className="muted xs" style={{ marginTop: 2 }}>{n.description}</div>}
      {(tags.length > 0 || links.length > 0) && (
        <div className="row" style={{ gap: 4, marginTop: 3, flexWrap: 'wrap' }}>
          {tags.map(t => <span key={t} className="chip xs" title="tag">#{t}</span>)}
          {links.length > 0 && <span className="muted xs" title="linked concepts (OKF edges)">🔗 {links.length}</span>}
        </div>
      )}
    </>
  );
}

const OKF_TYPE_BADGE: Record<string, string> = {
  objective: '🎯', key_result: '📊', learning: '🧠', session: '📎',
};

function OkrPanel() {
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
          <button className="ghost sm" disabled={busy} onClick={async () => {
            setBusy(true);
            try { const r = await api.memoryOkrMigrate(); toast.success(`Seeded ${r.migrated} topics into the graph`); load(); }
            catch (e: any) { toast.error(e.message); } finally { setBusy(false); }
          }} title="Seed the graph from existing topic briefs">Seed from briefs</button>
          <button className="ghost sm" onClick={load}>Refresh</button>
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
                        : <button className="ghost sm" onClick={() => setActive(kr.id)}>set active</button>}
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

function NotesPanel() {
  const [files, setFiles] = useState<any[] | null>(null);
  const [open, setOpen] = useState<any | null>(null);
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState('');
  const [caStep, setCaStep] = useState<string | null>(null);   // 'Compact all' progress

  const load = useCallback(() => {
    api.memoryFiles().then(setFiles).catch(() => setFiles([]));
  }, []);
  useEffect(() => { load(); }, [load]);

  async function view(name: string) {
    try { setOpen(await api.memoryFileGet(name)); } catch { /* ignore */ }
  }
  async function create() {
    if (!title.trim() || !text.trim()) return;
    setBusy(true);
    try {
      await api.memoryFileCreate({ title: title.trim(), text: text.trim(), kind: 'note' });
      setTitle(''); setText(''); setAdding(false); load();
    } finally { setBusy(false); }
  }
  async function del(name: string) {
    await api.memoryFileDelete(name); setOpen(null); load();
  }
  async function compact() {
    setBusy(true);
    try {
      const plan = await api.memoryFilesCompact({ group_by: 'topic', dry_run: true });
      const groups = Object.entries(plan.groups || {});
      if (!groups.length) { toast('Nothing to compact.'); return; }
      const summary = groups.map(([k, n]) => `${k} (${n})`).join(', ');
      if (!window.confirm(
        `Compact ${plan.files_in} files → ${plan.files_out} topic briefs?\n\n` +
        `Topics: ${summary}\n\nOriginals are archived (not deleted) and can be restored.`,
      )) return;
      const r = await api.memoryFilesCompact({ group_by: 'topic' });
      const how = (r.summarized && r.summarized.length)
        ? `LLM-summarized ${r.summarized.length}/${r.files_out}`
        : 'merged (no model reachable)';
      toast.success(`Compacted ${r.files_in} → ${r.files_out} files · ${how}`);
      load();
    } catch (e: any) {
      toast.error(`Compact failed: ${e.message}`);
    } finally { setBusy(false); }
  }
  async function compactAll() {
    if (!window.confirm('Compact ALL — redo everything from scratch: tidy legacy, re-run the LLM over EVERY brief, rebuild OKR repo cards, re-ingest. Heavy (full LLM pass, can take minutes). Continue?')) return;
    try {
      setCaStep('starting…');
      await api.memoryCompactAll();
      const t = toast.loading('Compact all: starting…');
      await new Promise<void>((resolve) => {
        const poll = setInterval(async () => {
          try {
            const s = await api.memoryCompactAllStatus();
            // step N/6 · <current step> · brief M/K (which brief) · elapsed
            const stepNo = Math.min(s.steps_done.length + 1, s.total_steps);
            const brief = s.sub && s.sub.total
              ? ` · brief ${s.sub.done}/${s.sub.total}${s.sub.key ? ` (${s.sub.key})` : ''}`
              : '';
            const short = s.sub && s.sub.total
              ? `${s.current} ${s.sub.done}/${s.sub.total}`
              : (s.current || 'working…');
            const label = s.running
              ? `Compact all — step ${stepNo}/${s.total_steps}: ${s.current || '…'}${brief} · ${s.elapsed_s}s`
              : 'Compact all: finishing…';
            setCaStep(short);
            toast.loading(label, { id: t });
            if (s.done || !s.running) {
              clearInterval(poll);
              setCaStep(null);
              if (s.error) toast.error(`Compact all failed: ${s.error}`, { id: t });
              else toast.success(`Compact all done · ${s.result?.topic?.files_out ?? 0} briefs, ${s.result?.repo_profiles?.profiles ?? 0} cards · ${s.elapsed_s}s`, { id: t });
              load();
              resolve();
            }
          } catch { /* keep polling */ }
        }, 2000);
      });
    } catch (e: any) {
      setCaStep(null);
      toast.error(`Compact all failed: ${e.message}`);
    }
  }

  return (
    <div className="card">
      <div className="card-header">
        <h2>Notes (markdown memory)</h2>
        <span className="muted small">
          Plain <code>.md</code> files in <code>~/.aiforge/memory</code> — written
          automatically after each chat run. Click a note to view it.
        </span>
      </div>
      <div className="row" style={{ gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <button onClick={() => setAdding(a => !a)}>{adding ? 'Cancel' : '+ Note'}</button>
        <button className="ghost" onClick={compact} disabled={busy}
                title="Fold only NEW/undone notes into topic briefs (originals archived, reversible)">
          Compact
        </button>
        <button className="ghost" onClick={compactAll} disabled={busy || caStep !== null}
                title="Redo EVERYTHING: tidy legacy + re-LLM every brief + rebuild OKR cards + re-ingest">
          {caStep !== null ? `⏳ ${caStep}` : 'Compact all'}
        </button>
        <button className="ghost" onClick={load}>Refresh</button>
        <input placeholder="filter by name / tag…" value={filter}
               onChange={e => setFilter(e.target.value)}
               style={{ marginLeft: 'auto', minWidth: 200 }} />
      </div>
      {adding && (
        <div className="row" style={{ flexDirection: 'column', gap: 6, marginBottom: 12 }}>
          <input placeholder="title" value={title} onChange={e => setTitle(e.target.value)} />
          <textarea placeholder="markdown body…" value={text}
                    onChange={e => setText(e.target.value)} rows={5} />
          <button onClick={create} disabled={busy || !title.trim() || !text.trim()}>
            {busy ? 'Saving…' : 'Save note'}
          </button>
        </div>
      )}
      {files === null ? <div className="muted small">Loading…</div>
        : files.length === 0 ? <div className="muted small">No notes yet — they appear after chat runs, or add one.</div>
        : (() => {
          const q = filter.trim().toLowerCase();
          const shown = files.filter(f => !q
            || cleanTitle(f).toLowerCase().includes(q)
            || String(f.name).toLowerCase().includes(q)
            || (f.tags || []).some((t: string) => String(t).toLowerCase().includes(q)));
          const byCat = new Map<Category, any[]>();
          for (const f of shown) {
            const c = categoryOf(f);
            (byCat.get(c) || byCat.set(c, []).get(c)!).push(f);
          }
          const cats = CATEGORY_ORDER.filter(c => (byCat.get(c) || []).length);
          if (!cats.length) return <div className="muted small">No matches.</div>;
          return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {cats.map(cat => {
                const rows = byCat.get(cat)!;
                return (
                  <details key={cat} open>
                    <summary style={{ cursor: 'pointer', fontWeight: 600, padding: '4px 0' }}>
                      {CAT_ICON[cat]} {cat} <span className="muted xs">({rows.length})</span>
                    </summary>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 4 }}>
                      {rows.map(f => {
                        const tags: string[] = (f.tags || []).filter((t: string) =>
                          !/^(doer-self-write|note|knowledge)$/i.test(t));
                        return (
                          <div key={f.name} className="row" style={{
                            gap: 8, alignItems: 'center', padding: '5px 8px',
                            borderRadius: 6, background: 'var(--bg-1)',
                          }}>
                            <a style={{ cursor: 'pointer', fontWeight: 500, minWidth: 200 }}
                               onClick={() => view(f.name)}>{cleanTitle(f)}</a>
                            <div className="row" style={{ gap: 4, flexWrap: 'wrap', flex: 1 }}>
                              {tags.slice(0, 6).map((t: string) => (
                                <span key={t} className="chip xs"
                                      style={{ cursor: 'pointer' }}
                                      onClick={() => setFilter(t)}>{t}</span>
                              ))}
                            </div>
                            <span className="muted xs" style={{ whiteSpace: 'nowrap' }}>
                              {(f.created || '').slice(0, 10)}
                            </span>
                            <button className="ghost sm" onClick={() => del(f.name)}
                                    title="Delete">✕</button>
                          </div>
                        );
                      })}
                    </div>
                  </details>
                );
              })}
            </div>
          );
        })()}
      {open && (
        <div onClick={() => setOpen(null)}
             style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      zIndex: 1000, padding: 20 }}>
          <div onClick={e => e.stopPropagation()}
               style={{ background: 'var(--bg-0)', border: '1px solid var(--border-1)',
                        borderRadius: 10, maxWidth: 820, width: '100%', maxHeight: '85vh',
                        overflow: 'auto', padding: 16, boxShadow: '0 12px 48px rgba(0,0,0,0.45)' }}>
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
              <strong>{open.title}</strong>
              <button className="ghost sm" onClick={() => setOpen(null)}><Icon.X size={14} /> close</button>
            </div>
            <div className="small muted" style={{ margin: '4px 0' }}>{open.file}</div>
            <pre style={{ whiteSpace: 'pre-wrap', fontSize: 13, margin: 0 }}>{open.body}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Hybrid search (the SAME engine agents use: semantic KNN + keyword + spell) ──
function SearchPanel() {
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<any[] | null>(null);
  const [busy, setBusy] = useState(false);

  const run = useCallback(async () => {
    const query = q.trim();
    if (query.length < 2) return;
    setBusy(true);
    try {
      setHits(await api.memorySearch(query, 'planner', 12));
    } catch (e: any) {
      toast.error('Search failed: ' + (e?.message || e));
    } finally {
      setBusy(false);
    }
  }, [q]);

  return (
    <div className="card">
      <div className="card-header">
        <h2>Search memory</h2>
        <span className="muted small">
          Same hybrid recall the agents use — semantic nearest-neighbour
          (sqlite-vec) + keyword (BM25) + spell-correction, fused. Try a
          paraphrase (“how do we ship a release”) or an exact id.
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
        <button onClick={run} disabled={busy || q.trim().length < 2}>
          {busy ? 'Searching…' : 'Search'}
        </button>
      </div>
      {hits && hits.length === 0 && <div className="muted small">No matches.</div>}
      {hits && hits.length > 0 && (
        <div className="col" style={{ gap: 8 }}>
          {hits.map((h, i) => (
            <div key={i} className="card" style={{ padding: '10px 12px' }}>
              <div className="row small muted" style={{ gap: 8, marginBottom: 4 }}>
                <span className="pill">{h.wing || 'memory'}</span>
                {h.source && <span>{truncate(h.source, 32)}</span>}
                {h.metadata?.repo && <span>· {h.metadata.repo}</span>}
                {typeof h.score === 'number' && (
                  <span style={{ marginLeft: 'auto' }}>score {h.score.toFixed(2)}</span>
                )}
              </div>
              <div style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>{h.text}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Memory() {
  return (
    <>
      <div className="page-header">
        <div>
          <h1>Memory</h1>
          <div className="subtitle">Goal graph, indexed sources, and human-readable notes.</div>
        </div>
      </div>

      {/* Hybrid search — same engine the agents use */}
      <SearchPanel />

      {/* OKR-DAG — the goal-oriented memory (primary) */}
      <OkrPanel />

      {/* Per-datasource overview + clear */}
      <OverviewPanel />

      {/* Markdown notes (auto-written after chat runs) */}
      <NotesPanel />

      {/* Sources management */}
      <SourcesPanel />
    </>
  );
}
