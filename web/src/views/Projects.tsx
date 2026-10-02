import { useEffect, useRef, useState } from 'react';
import { NavLink, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import { projectsApi, type FolderHit, type Project, type ProjectList } from '../api';
import { Icon } from '../icons';
import Chat from './Chat';

function relDay(iso?: string): string {
  if (!iso) return 'no chats yet';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const days = Math.floor((Date.now() - t) / 86_400_000);
  if (days <= 0) return 'active today';
  if (days === 1) return 'active yesterday';
  return `active ${days} days ago`;
}

function memoryLabel(p: Project): string {
  if (!p.memory_chars) return 'no memory yet';
  const pct = Math.min(100, Math.round((p.memory_chars / Math.max(1, p.memory_cap)) * 100));
  return `memory ${pct}% of cap`;
}

/** Type a folder path, get the matching folders as you type. Tab or a click
 *  fills the path in (and keeps suggesting inside it); Enter opens it. */
function OpenFolder({ onOpen }: { onOpen: (path: string) => void }) {
  const [text, setText] = useState('');
  const [hits, setHits] = useState<FolderHit[]>([]);
  const [active, setActive] = useState(0);
  const [show, setShow] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    const h = setTimeout(() => {
      projectsApi.browse(text, ctrl.signal)
        .then(r => { setHits(r.folders); setActive(0); })
        .catch(() => { /* typing on: the next keystroke asks again */ });
    }, 120);
    return () => { clearTimeout(h); ctrl.abort(); };
  }, [text]);

  function fill(hit: FolderHit) {
    // Trailing slash: the next suggestions are the folders inside this one.
    setText(hit.path.endsWith('/') ? hit.path : `${hit.path}/`);
    setShow(true);
    input.current?.focus();
  }

  function open(path: string) {
    const clean = path.length > 1 ? path.replace(/\/+$/, '') : path;
    if (clean) onOpen(clean);
  }

  function onKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown') { e.preventDefault(); setShow(true); setActive(i => Math.min(i + 1, hits.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(i => Math.max(i - 1, 0)); }
    else if (e.key === 'Tab' && show && hits[active]) { e.preventDefault(); fill(hits[active]); }
    else if (e.key === 'Escape') { setShow(false); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const exact = hits.find(h => h.path === text.replace(/\/+$/, ''));
      // A bare name (no slash) opens the highlighted match; a path opens as typed.
      if (!text.startsWith('/') && !text.startsWith('~') && hits[active]) open(hits[active].path);
      else if (exact ? exact.openable : text.trim()) open(text.trim());
    }
  }

  return (
    <div style={{ position: 'relative', minWidth: 360, flex: 1, maxWidth: 620 }}>
      <input ref={input} value={text} placeholder="Open a folder — type a name or a path, e.g. /home/me/code/shop"
             onChange={e => { setText(e.target.value); setShow(true); }}
             onFocus={() => setShow(true)}
             onBlur={() => setTimeout(() => setShow(false), 150)}
             onKeyDown={onKey} spellCheck={false} autoComplete="off"
             aria-label="Open a folder as a project" aria-autocomplete="list"
             style={{ width: '100%', fontFamily: 'var(--font-mono)' }} />
      {show && hits.length > 0 && (
        <div role="listbox" style={{
          position: 'absolute', zIndex: 20, top: '100%', left: 0, right: 0, marginTop: 4,
          background: 'var(--bg-0)', border: '1px solid var(--border-1)', borderRadius: 8,
          boxShadow: '0 8px 28px rgba(0,0,0,0.25)', maxHeight: 320, overflow: 'auto' }}>
          {hits.map((h, i) => (
            <div key={h.path} role="option" aria-selected={i === active}
                 onMouseDown={e => { e.preventDefault(); fill(h); }}
                 onMouseEnter={() => setActive(i)}
                 style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', cursor: 'pointer',
                          background: i === active ? 'var(--bg-1)' : 'transparent' }}>
              <Icon.Folder size={13} />
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: 13, flex: 1, overflow: 'hidden',
                             textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{h.path}</span>
              {h.is_git && <span className="chip">git</span>}
              {h.openable
                ? <button type="button" className="ghost sm"
                          onMouseDown={e => { e.preventDefault(); e.stopPropagation(); open(h.path); }}>
                    Open
                  </button>
                : <span className="muted xs">go inside</span>}
            </div>
          ))}
          <div className="muted xs" style={{ padding: '4px 10px', borderTop: '1px solid var(--border-1)' }}>
            ↑↓ choose · Tab fill in · Enter open
          </div>
        </div>
      )}
    </div>
  );
}

const LS_LAST_PROJECT = 'aiforge.projects.last';

/** Operate → Projects, one page: your projects across the top, "New project"
 *  to add a folder, and the selected project's chat right below. */
export default function Projects() {
  const [data, setData] = useState<ProjectList | null>(null);
  const [adding, setAdding] = useState(false);
  const [opening, setOpening] = useState('');
  const [params, setParams] = useSearchParams();
  const selectedPath = params.get('path') || '';
  // The opened project (registered server-side) the chat below is bound to.
  const [active, setActive] = useState<Project | null>(null);

  function load(): Promise<ProjectList | null> {
    return projectsApi.list().then(d => { setData(d); return d; }).catch((e: any) => {
      toast.error(`Failed to load projects: ${e.message}`);
      const empty = { root: '', exists: false, projects: [] };
      setData(empty);
      return empty;
    });
  }

  function select(path: string) {
    const next = new URLSearchParams(params);
    if (path) next.set('path', path); else next.delete('path');
    setParams(next, { replace: false });
  }

  // First load: no project in the URL → the last one used, else the most
  // recent of yours.
  useEffect(() => {
    load().then(d => {
      if (selectedPath || !d) return;
      const mine = d.projects.filter(p => p.mine);
      let last = '';
      try { last = localStorage.getItem(LS_LAST_PROJECT) || ''; } catch { /* storage off */ }
      const pick = mine.find(p => p.path === last) || mine[0];
      if (pick) select(pick.path);
      else setAdding(true);                    // nothing yet: start at New project
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Open (register + sync memory) whatever the URL selects, then show its chat.
  useEffect(() => {
    if (!selectedPath) { setActive(null); return; }
    let live = true;
    setOpening(selectedPath);
    projectsApi.openPath(selectedPath).then(p => {
      if (!live) return;
      setActive(p);
      setAdding(false);
      try { localStorage.setItem(LS_LAST_PROJECT, p.path); } catch { /* storage off */ }
      load();
    }).catch((e: any) => {
      if (!live) return;
      setActive(null);
      toast.error(e.message || 'That folder cannot be opened');
      select('');
    }).finally(() => { if (live) setOpening(''); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedPath]);

  async function remove(p: Project) {
    if (!window.confirm(`Take “${p.name}” off your projects?\n\nIts chats and memory are kept. `
      + 'You can add it again with New project.')) return;
    try {
      await projectsApi.remove(p.path);
      if (selectedPath === p.path) select('');
      await load();
    } catch (e: any) { toast.error(e.message); }
  }

  const mine = (data?.projects || []).filter(p => p.mine);
  const others = (data?.projects || []).filter(p => !p.mine);

  return (
    <div className="projects-page">
      <div className="projects-bar">
        <span className="projects-bar-title">Your projects</span>
        <div className="projects-pills">
          {data === null && <span className="muted small">Loading…</span>}
          {data !== null && mine.length === 0 && (
            <span className="muted small">None yet — add one with New project.</span>
          )}
          {mine.map(p => (
            <span key={p.path} className={`project-pill${p.path === selectedPath ? ' active' : ''}`}
                  title={`${p.path}\n${p.chats || 0} chats · ${relDay(p.last_activity)} · ${memoryLabel(p)}`}>
              <button type="button" className="project-pill-main" onClick={() => select(p.path)}>
                <Icon.Folder size={13} /> {p.name}
                <span className="muted xs">{p.chats || 0}</span>
              </button>
              <button type="button" className="project-pill-x" onClick={() => remove(p)}
                      title="Take off your projects (chats and memory are kept)" aria-label={`Remove ${p.name}`}>
                ✕
              </button>
            </span>
          ))}
        </div>
        <button type="button" className={adding ? 'ghost' : ''} onClick={() => setAdding(a => !a)}
                style={{ whiteSpace: 'nowrap' }}>
          {adding ? 'Close' : <><Icon.Plus size={13} /> New project</>}
        </button>
      </div>

      {adding && (
        <div className="card" style={{ marginBottom: 10 }}>
          <strong>New project</strong>
          <div className="muted small" style={{ margin: '4px 0 10px' }}>
            Pick the folder of a repo. Type its name or path — suggestions appear as you type
            (Tab fills one in, Enter opens it).
          </div>
          <OpenFolder onOpen={select} />
          {others.length > 0 && (
            <>
              <div className="muted xs" style={{ margin: '12px 0 6px' }}>
                Found in {(data?.roots || []).map(r => r.path).join(' · ') || 'your folders'}
              </div>
              <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                {others.map(p => (
                  <button type="button" key={p.path} className="ghost sm" title={p.path}
                          onClick={() => select(p.path)}>
                    <Icon.Folder size={12} /> {p.name}{p.is_git ? '' : ' · not git'}
                  </button>
                ))}
              </div>
            </>
          )}
          {data !== null && others.length === 0 && mine.length === 0 && (
            <div className="muted small" style={{ marginTop: 10 }}>
              No folders found. In Docker, AIForge sees its projects folder and the folders you
              mounted — add one under Settings → Mounts (or run with --repos DIR / --mount DIR),
              then restart. <NavLink to="/">Open settings</NavLink>
            </div>
          )}
        </div>
      )}

      {active && active.path === selectedPath
        ? <Chat key={active.path} project={{ name: active.name, path: active.path }} />
        : !adding && (
          <div className="card muted small">
            {opening ? `Opening ${opening}…` : 'Select one of your projects, or add one with New project.'}
          </div>
        )}
    </div>
  );
}

/** Old link form `/projects/<name>[?path=…]` → the one-page Projects view. */
export function ProjectChat() {
  const { name = '' } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  useEffect(() => {
    const path = params.get('path');
    if (path) { navigate(`/projects?path=${encodeURIComponent(path)}`, { replace: true }); return; }
    projectsApi.open(name)
      .then(p => navigate(`/projects?path=${encodeURIComponent(p.path)}`, { replace: true }))
      .catch(() => navigate('/projects', { replace: true }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);
  return <div className="muted small" style={{ padding: 24 }}>Opening {name}…</div>;
}
