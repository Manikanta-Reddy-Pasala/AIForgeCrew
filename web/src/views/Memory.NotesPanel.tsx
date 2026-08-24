import { useState, useEffect, useCallback } from 'react';
import { toast } from 'sonner';
import { api } from '../api';
import { Icon } from '../icons';
import { Category, CATEGORY_ORDER, CAT_ICON, categoryOf, cleanTitle } from './Memory.helpers';
import { clickable } from '../a11y';

export function NotesPanel() {
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
                               {...clickable(() => view(f.name))}>{cleanTitle(f)}</a>
                            <div className="row" style={{ gap: 4, flexWrap: 'wrap', flex: 1 }}>
                              {tags.slice(0, 6).map((t: string) => (
                                <span key={t} className="chip xs"
                                      style={{ cursor: 'pointer' }}
                                      {...clickable(() => setFilter(t))}>{t}</span>
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
        <div {...clickable(() => setOpen(null))}
             aria-label="Close"
             style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      zIndex: 1000, padding: 20 }}>
          <div onClick={e => e.stopPropagation()}
               onKeyDown={e => e.stopPropagation()}
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
