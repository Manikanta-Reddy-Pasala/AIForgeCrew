import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import { projectsApi, type Project, type ProjectMemory } from '../api';

function when(ts?: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleString() : 'never';
}

/** Per-project memory: read and edit the brief, see how full it is, clear out
 *  stale facts, move a fact to global, or forget the project. */
export function ProjectsPanel() {
  const [params, setParams] = useSearchParams();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [name, setName] = useState(params.get('project') || '');
  const [mem, setMem] = useState<ProjectMemory | null>(null);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState('');
  const editor = useRef<HTMLTextAreaElement>(null);

  function loadList() {
    projectsApi.withMemory().then(setProjects).catch(() => setProjects([]));
  }

  async function load(n: string) {
    if (!n) { setMem(null); setText(''); return; }
    try {
      const m = await projectsApi.memory(n);
      setMem(m);
      setText(m.text || '');
    } catch (e: any) {
      toast.error(`Could not load ${n} memory: ${e.message}`);
      setMem(null);
    }
  }

  useEffect(() => { loadList(); }, []);
  useEffect(() => { load(name); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [name]);

  function pick(n: string) {
    setName(n);
    const next = new URLSearchParams(params);
    if (n) next.set('project', n); else next.delete('project');
    setParams(next, { replace: true });
  }

  async function run(label: string, fn: () => Promise<void>) {
    setBusy(label);
    try { await fn(); } catch (e: any) { toast.error(`${label} failed: ${e.message}`); }
    finally { setBusy(''); }
  }

  const save = () => run('Save', async () => {
    await projectsApi.saveMemory(name, text);
    toast.success('Project memory saved');
    await load(name); loadList();
  });

  const compact = () => run('Compact', async () => {
    const r = await projectsApi.compact(name);
    if (r.compacted) {
      toast.success(`Compacted: ${r.chars_before} → ${r.chars_after} chars. Old version archived.`);
    } else if (r.error) {
      toast.warning(`Not compacted: ${r.error}`);
    } else {
      toast.success(`Nothing to fold. ${r.stale || 0} stale fact(s) moved out.`);
    }
    await load(name); loadList();
  });

  const promote = () => run('Move to global', async () => {
    const el = editor.current;
    const picked = el ? el.value.slice(el.selectionStart, el.selectionEnd).trim() : '';
    if (!picked) { toast.warning('Select the lines to move first.'); return; }
    const r = await projectsApi.promote(name, picked);
    toast.success(`Moved ${r.moved} line(s) to global memory`);
    await load(name);
  });

  const forget = () => run('Forget', async () => {
    if (!window.confirm(`Forget all memory for ${name}?\n\nThe brief is archived under ~/.aiforge/memory/archive, `
      + 'its search rows and the MEMORY.md file in the repo are removed.')) return;
    await projectsApi.forget(name);
    toast.success(`${name} memory forgotten (archived)`);
    pick(''); loadList();
  });

  const stale = (fact: string, action: 'restore' | 'delete') => run(action, async () => {
    await projectsApi.stale(name, fact, action);
    await load(name); loadList();
  });

  const dirty = !!mem && text !== (mem.text || '');
  const pct = mem ? Math.min(100, Math.round((mem.chars / Math.max(1, mem.cap)) * 100)) : 0;

  return (
    <div className="card">
      <div className="card-header">
        <h2>Project memory</h2>
        <span className="muted small">
          What chats learned about one repo. Kept in <code>.aiforge/memory/MEMORY.md</code> inside
          the repo, so you can read, edit and commit it.
        </span>
      </div>
      <div className="row" style={{ gap: 8, marginBottom: 10, flexWrap: 'wrap', alignItems: 'center' }}>
        <select value={name} onChange={e => pick(e.target.value)} style={{ minWidth: 220 }}>
          <option value="">Select a project…</option>
          {name && !(projects || []).some(p => p.name === name) && <option value={name}>{name}</option>}
          {(projects || []).map(p => <option key={p.name} value={p.name}>{p.name}</option>)}
        </select>
        {mem && (
          <span className="muted small">
            {mem.chars.toLocaleString()} / {mem.cap.toLocaleString()} chars ({pct}%) ·
            last compacted {when(mem.compacted_at)}
          </span>
        )}
      </div>
      {projects !== null && projects.length === 0 && !name && (
        <div className="muted small">No project has memory yet. Open one from Projects and chat on it.</div>
      )}
      {mem && (
        <>
          {!mem.writable && (
            <div className="small" style={{ marginBottom: 8, color: 'var(--warn, #b7791f)' }}>
              The repo is read-only, so this memory is stored in ~/.aiforge only.
            </div>
          )}
          <div className="muted xs" style={{ marginBottom: 6, fontFamily: 'var(--font-mono)' }}>{mem.repo_file}</div>
          <textarea ref={editor} value={text} onChange={e => setText(e.target.value)} rows={16}
                    placeholder="Nothing learned yet."
                    style={{ width: '100%', fontFamily: 'var(--font-mono)', fontSize: 13 }} />
          <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <button type="button" onClick={save} disabled={!dirty || !!busy}>
              {busy === 'Save' ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="ghost" onClick={compact} disabled={!!busy || dirty}
                    title="Move facts about deleted files out, then fold the brief into a shorter one. The old version is archived.">
              {busy === 'Compact' ? 'Compacting…' : 'Compact now'}
            </button>
            <button type="button" className="ghost" onClick={promote} disabled={!!busy || dirty}
                    title="Select lines in the editor, then move them to global memory">
              Move selection to global
            </button>
            <button type="button" className="danger" onClick={forget} disabled={!!busy}
                    style={{ marginLeft: 'auto' }}>
              Forget this project
            </button>
          </div>
          {mem.stale.length > 0 && (
            <details open style={{ marginTop: 12 }}>
              <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
                Stale <span className="muted xs">({mem.stale.length}) — these name files that no longer exist, and are not used</span>
              </summary>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 6 }}>
                {mem.stale.map(s => (
                  <div key={s.fact} className="row" style={{ gap: 8, alignItems: 'center', padding: '5px 8px',
                                                            borderRadius: 6, background: 'var(--bg-1)' }}>
                    <span style={{ flex: 1 }}>{s.fact}</span>
                    <button type="button" className="ghost sm" disabled={!!busy}
                            onClick={() => stale(s.fact, 'restore')}>Restore</button>
                    <button type="button" className="ghost sm" disabled={!!busy}
                            onClick={() => stale(s.fact, 'delete')}>Delete</button>
                  </div>
                ))}
              </div>
            </details>
          )}
        </>
      )}
    </div>
  );
}
