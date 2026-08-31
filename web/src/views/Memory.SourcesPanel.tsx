import { useState, useEffect, useRef, useCallback } from 'react';
import { toast } from 'sonner';
import { api, MemorySource } from '../api';
import { KIND_OPTIONS, statusClass, relativeDate, truncate } from './Memory.helpers';

// ─── Sources management panel ─────────────────────────────────────────────────

export function SourcesPanel() {
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
          {sources.length} source{sources.length !== 1 ? 's' : ''} · add markdown or code — indexed on the go (RepoMap + CodeGraph for code relations)
        </span>
      </div>

      {/* ── Add source form ── */}
      <div style={{ marginBottom: 20, padding: '14px 16px', background: 'var(--bg-1)', borderRadius: 'var(--r-md)', border: '1px solid var(--border-0)' }}>
        <div style={{ marginBottom: 10, fontWeight: 600, fontSize: 'var(--fs-sm)', color: 'var(--fg-0)' }}>Add source</div>
        <div className="row" style={{ flexWrap: 'wrap', gap: 8, alignItems: 'flex-end' }}>
          <label className="field" style={{ minWidth: 130 }}>
            Kind{' '}
            <select value={kind} onChange={e => { setKind(e.target.value); setLocation(''); setFile(null); setName(''); }}>
              {KIND_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>

          {kind === 'file' ? (
            <label className="field" style={{ flex: 1, minWidth: 200 }}>
              File{' '}
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
            <button type="button" onClick={validatePath} disabled={validating} className="ghost sm"
                    title="Check the path resolves + has files BEFORE indexing"
                    style={{ alignSelf: 'flex-end', marginBottom: 1 }}>
              {validating ? 'Checking…' : 'Validate path'}
            </button>
          )}
          <button type="button" onClick={handleAdd} disabled={adding} style={{ alignSelf: 'flex-end', marginBottom: 1 }}>
            {adding ? 'Adding…' : 'Add'}
          </button>
        </div>
      </div>

      {/* ── Sources table ── */}
      {(() => {
        if (loading) return (
        <div className="stack">
          {[1, 2].map(i => <div key={i} className="skeleton" style={{ height: 40, borderRadius: 6 }} />)}
        </div>
        );
        if (sources.length === 0) return (
        <div className="empty" style={{ padding: '32px 16px' }}>
          <div className="empty-icon">∅</div>
          <div style={{ color: 'var(--fg-0)', fontWeight: 500 }}>No sources yet</div>
          <div>Add a repo, docs folder, URL, or file above.</div>
        </div>
        );
        return (
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
                      <button type="button"
                        className="ghost sm"
                        disabled={s.status === 'indexing'}
                        onClick={() => handleIndex(s.id)}
                        title="Re-index this source"
                      >
                        Index
                      </button>
                      <button type="button"
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
        );
      })()}
    </div>
  );
}
