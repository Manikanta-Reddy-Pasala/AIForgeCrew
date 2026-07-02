import { useState, useEffect, useRef, useCallback } from 'react';
import { toast } from 'sonner';
import { api, MemorySource, MemoryOverview, MemoryStoreSection } from '../api';
import { Icon } from '../icons';

const ROLES = ['supervisor', 'planner', 'doer', 'feedback', 'learner'];

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

// ─── Indexed memory stats panel ──────────────────────────────────────────────

function IndexedMemoryPanel() {
  const [stats, setStats] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.memoryStats()
      .then(setStats)
      .catch(() => { /* silently fail — backend may not be running */ })
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="card">
      <div className="card-header">
        <h2>Indexed memory</h2>
        {stats && (
          <span className="muted small">
            backend: <code>{stats.backend}</code> &nbsp;·&nbsp; total: <strong>{stats.total?.toLocaleString()}</strong>
          </span>
        )}
      </div>

      {loading && (
        <div className="row" style={{ gap: 8, padding: '8px 0' }}>
          {[1, 2, 3, 4].map(i => (
            <div key={i} className="skeleton" style={{ height: 56, flex: 1, borderRadius: 8 }} />
          ))}
        </div>
      )}

      {!loading && !stats && (
        <div className="muted small">Could not load stats — backend may be offline.</div>
      )}

      {!loading && stats && (
        <>
          {(!stats.wings || stats.wings.length === 0) ? (
            <div className="muted small">No data indexed yet.</div>
          ) : (
            <div className="mem-wings-grid">
              {stats.wings.map((w: any) => (
                <div key={w.wing} className="mem-wing-pill">
                  <span className="wing-label" title={w.wing}>{w.wing}</span>
                  <span className="wing-count">{(w.n ?? 0).toLocaleString()}</span>
                  {w.embedded != null && (
                    <span className="muted xs">{w.embedded.toLocaleString()} embedded</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
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
    key: 'graph_facts', label: 'Neo4j graph — facts',
    hint: 'observations / decisions / facts',
    summary: s => labelSummary(s, 'nodes'),
  },
  {
    key: 'symbols', label: 'Tree-sitter symbols',
    hint: 'code symbols + call/extends/implements edges',
    summary: s => {
      const nodes = (s.total ?? 0).toLocaleString();
      const rels = Object.values(s.relationships || {}).reduce((a, b) => a + b, 0);
      return `${nodes} symbol nodes` + (rels ? `, ${rels.toLocaleString()} edges` : '');
    },
  },
  {
    key: 'graphify', label: 'Graphify',
    hint: "graphify-tagged nodes (source='graphify')",
    summary: s => `${(s.count ?? 0).toLocaleString()} nodes`,
  },
  {
    key: 'chunks', label: 'Code / doc chunks',
    hint: 'embedded content chunks',
    summary: s => labelSummary(s, 'chunks'),
  },
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

function labelSummary(s: MemoryStoreSection, unit: string): string {
  const total = (s.total ?? 0).toLocaleString();
  const parts = Object.entries(s.labels || {})
    .map(([k, v]) => `${k} ${v}`).join(', ');
  return `${total} ${unit}` + (parts ? ` — ${parts}` : '');
}

function OverviewPanel() {
  const [ov, setOv] = useState<MemoryOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setOv(await api.memoryOverview()); }
    catch { /* backend may be offline */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

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
        <h2>Memory overview</h2>
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
      </div>

      {loading && (
        <div className="row" style={{ gap: 8, padding: '8px 0' }}>
          {[1, 2, 3].map(i => (
            <div key={i} className="skeleton" style={{ height: 48, flex: 1, borderRadius: 8 }} />
          ))}
        </div>
      )}

      {!loading && !ov && (
        <div className="muted small">Could not load overview — backend may be offline.</div>
      )}

      {!loading && ov && (
        <div className="stack" style={{ gap: 8 }}>
          {OVERVIEW_STORES.map(store => {
            const s = ov.stores[store.key] || {};
            const unavailable = s.available === false;
            return (
              <div
                key={store.key}
                className="row"
                style={{
                  justifyContent: 'space-between', alignItems: 'center',
                  padding: '8px 0', borderBottom: '1px solid var(--border-0)',
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
                <button
                  className="ghost danger"
                  onClick={() => clearStore(store.key, store.label)}
                  disabled={busy !== null || unavailable}
                  title={unavailable ? 'store unavailable' : `Empty ${store.label}`}
                >
                  {busy === store.key ? 'Clearing…' : <><Icon.Trash size={13} /> Empty this</>}
                </button>
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
        <span className="muted small">{sources.length} source{sources.length !== 1 ? 's' : ''}</span>
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

function NotesPanel() {
  const [files, setFiles] = useState<any[] | null>(null);
  const [open, setOpen] = useState<any | null>(null);
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);

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
      const plan = await api.memoryFilesCompact({ group_by: 'kind', dry_run: true });
      const groups = Object.entries(plan.groups || {});
      if (!groups.length) { toast('Nothing to compact — no group has 2+ files.'); return; }
      const summary = groups.map(([k, n]) => `${k} (${n})`).join(', ');
      if (!window.confirm(
        `Compact ${plan.files_in} files → ${plan.files_out} grouped files?\n\n` +
        `Groups: ${summary}\n\nOriginals are archived (not deleted) and can be restored.`,
      )) return;
      const r = await api.memoryFilesCompact({ group_by: 'kind' });
      const how = (r.summarized && r.summarized.length)
        ? `LLM-summarized ${r.summarized.length}/${r.files_out}`
        : 'merged (no model reachable)';
      toast.success(`Compacted ${r.files_in} → ${r.files_out} files · ${how}`);
      load();
    } catch (e: any) {
      toast.error(`Compact failed: ${e.message}`);
    } finally { setBusy(false); }
  }

  return (
    <div className="card">
      <div className="card-header">
        <h2>Notes (markdown memory)</h2>
        <span className="muted small">
          Plain <code>.md</code> files in <code>~/.aiforge/memory</code> — written
          automatically after each chat run, also searchable above.
        </span>
      </div>
      <div className="row" style={{ gap: 8, marginBottom: 10 }}>
        <button onClick={() => setAdding(a => !a)}>{adding ? 'Cancel' : '+ Note'}</button>
        <button className="ghost" onClick={() => api.memoryFilesIngest().then(load)}>
          Re-ingest folder
        </button>
        <button className="ghost" onClick={compact} disabled={busy}
                title="Group per-session notes into fewer standardized .md files (originals archived)">
          Compact
        </button>
        <button className="ghost" onClick={load}>Refresh</button>
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
        : (
          <table className="table">
            <thead><tr><th>Title</th><th>Kind</th><th>Source</th><th>Created</th><th /></tr></thead>
            <tbody>
              {files.map(f => (
                <tr key={f.name}>
                  <td><a style={{ cursor: 'pointer' }} onClick={() => view(f.name)}>{f.title}</a></td>
                  <td><span className="chip sm">{f.kind}</span></td>
                  <td className="small muted">{f.source}</td>
                  <td className="small muted">{(f.created || '').slice(0, 16).replace('T', ' ')}</td>
                  <td><button className="ghost sm" onClick={() => del(f.name)}>✕</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      {open && (
        <div style={{ marginTop: 12, padding: 12, background: 'var(--bg-1)',
                      border: '1px solid var(--border-0)', borderRadius: 8 }}>
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <strong>{open.title}</strong>
            <button className="ghost sm" onClick={() => setOpen(null)}>close</button>
          </div>
          <div className="small muted" style={{ margin: '4px 0' }}>{open.file}</div>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>{open.body}</pre>
        </div>
      )}
    </div>
  );
}

export default function Memory() {
  const [q, setQ]         = useState('');
  const [role, setRole]   = useState('planner');
  const [hits, setHits]   = useState<any[] | null>(null);
  const [loading, setLoading] = useState(false);

  async function search() {
    if (q.trim().length < 2) return;
    setLoading(true);
    try {
      const r = await api.memorySearch(q, role, 15);
      setHits(r);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Memory</h1>
          <div className="subtitle">Manage memory sources, view what's indexed, and search across all wings.</div>
        </div>
      </div>

      {/* Per-datasource overview + clear */}
      <OverviewPanel />

      {/* Indexed memory stats */}
      <IndexedMemoryPanel />

      {/* Markdown notes (auto-written after chat runs) */}
      <NotesPanel />

      {/* Sources management */}
      <SourcesPanel />

      {/* Search */}
      <div className="card">
        <div className="card-header">
          <h2>Search</h2>
          <span className="muted small">Hybrid vector + BM25 across T1–T4, scoped to a role profile</span>
        </div>
        <div className="row">
          <div className="input-search" style={{ flex: 1, minWidth: 300 }}>
            <Icon.Search size={14} />
            <input
              placeholder="query (e.g. stock transfer sync rules)"
              value={q}
              onChange={e => setQ(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && search()}
              autoFocus
            />
          </div>
          <label className="field" style={{ flexDirection: 'row', alignItems: 'center' }}>
            Role
            <select value={role} onChange={e => setRole(e.target.value)} style={{ minWidth: 130 }}>
              {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <button onClick={search} disabled={loading || q.trim().length < 2}>
            {loading ? 'Searching…' : <><Icon.Search size={14} /> Search</>}
          </button>
        </div>
      </div>

      {hits !== null && (
        hits.length === 0 ? (
          <div className="card">
            <div className="empty">
              <div className="empty-icon">∅</div>
              <div style={{ color: 'var(--fg-0)', fontWeight: 500 }}>No hits</div>
              <div>Try a broader query or a different role.</div>
            </div>
          </div>
        ) : (
          <div className="card">
            <div className="card-header">
              <h2>{hits.length} hits</h2>
              <span className="muted small">role: <code>{role}</code></span>
            </div>
            <div className="stack">
              {hits.map((h: any, i: number) => (
                <div key={i} style={{ paddingBottom: 12, borderBottom: '1px solid var(--border-0)' }}>
                  <div className="row tight" style={{ marginBottom: 6 }}>
                    <span className="chip sm">{h.tier}</span>
                    <span className="chip sm mono">{h.wing}</span>
                    <span className="muted xs mono">score {Number(h.score).toFixed(3)}</span>
                    {h.source && <span className="muted xs">· {h.source}</span>}
                  </div>
                  <pre style={{ margin: 0, fontSize: 12 }}>{(h.text || '').slice(0, 1500)}</pre>
                </div>
              ))}
            </div>
          </div>
        )
      )}
    </>
  );
}
