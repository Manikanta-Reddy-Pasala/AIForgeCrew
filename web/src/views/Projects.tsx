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

function projectUrl(p: { name: string; path: string }): string {
  return `/projects/${encodeURIComponent(p.name)}?path=${encodeURIComponent(p.path)}`;
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

/** Operate → Projects: the folders under the mounted repos path. Opening one
 *  starts chats that already know the repo. */
export default function Projects() {
  const [data, setData] = useState<ProjectList | null>(null);
  const [filter, setFilter] = useState('');
  const navigate = useNavigate();

  useEffect(() => {
    projectsApi.list().then(setData).catch((e: any) => {
      toast.error(`Failed to load projects: ${e.message}`);
      setData({ root: '', exists: false, projects: [] });
    });
  }, []);

  async function openPath(path: string) {
    try {
      const p = await projectsApi.openPath(path);
      navigate(projectUrl(p));
    } catch (e: any) {
      toast.error(e.message || 'That folder cannot be opened');
    }
  }

  const q = filter.trim().toLowerCase();
  const shown = (data?.projects || []).filter(p => !q
    || p.name.toLowerCase().includes(q) || p.path.toLowerCase().includes(q));

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Projects</h1>
          <div className="subtitle">
            Pick a repo folder to chat on. The chat starts with what earlier chats
            on that project learned.
          </div>
        </div>
      </div>

      <div className="row" style={{ gap: 10, marginBottom: 14, flexWrap: 'wrap', alignItems: 'center' }}>
        <OpenFolder onOpen={openPath} />
        <input placeholder="filter the list…" value={filter}
               onChange={e => setFilter(e.target.value)}
               style={{ width: 200, flex: '0 0 auto' }} />
      </div>
      {data?.roots && data.roots.length > 0 && (
        <div className="muted xs" style={{ marginBottom: 12 }}>
          Looking in: {data.roots.map(r => r.path).join(' · ')}
        </div>
      )}

      {data === null && <div className="muted small">Loading…</div>}

      {data !== null && data.projects.length === 0 && (
        <div className="card">
          <strong>No project folders found yet.</strong>
          <div className="muted small" style={{ marginTop: 6 }}>
            Type a path in the box above to open any folder AIForge can see. In Docker it
            sees the projects folder and the folders you mounted — add one under
            Settings → Mounts (or run with --repos DIR / --mount DIR), then restart.
            {' '}<NavLink to="/">Open settings</NavLink>
          </div>
        </div>
      )}

      <div style={{ display: 'grid', gap: 12,
                    gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))' }}>
        {shown.map(p => (
          <button type="button" key={p.path} className="card"
                  onClick={() => navigate(projectUrl(p))}
                  title={p.path}
                  style={{ textAlign: 'left', cursor: 'pointer', display: 'flex',
                           flexDirection: 'column', gap: 6 }}>
            <span style={{ fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6 }}>
              <Icon.Folder size={15} /> {p.name}
            </span>
            <span className="muted xs">
              {p.chats || 0} {p.chats === 1 ? 'chat' : 'chats'} · {relDay(p.last_activity)}
            </span>
            <span className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
              <span className="chip">{memoryLabel(p)}</span>
              {p.stale > 0 && <span className="chip warn">{p.stale} stale</span>}
              {!p.is_git && <span className="chip">not a git repo</span>}
              {p.registered && !p.writable && (
                <span className="chip warn" title="The repo is read-only, so the memory file stays in ~/.aiforge">
                  memory kept outside repo
                </span>
              )}
            </span>
          </button>
        ))}
      </div>
    </>
  );
}

/** /projects/<name> — the Chat screen bound to one project folder. */
export function ProjectChat() {
  const { name = '' } = useParams();
  const [params] = useSearchParams();
  const path = params.get('path') || '';
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    setProject(null);
    setError('');
    // The path says exactly which folder; the name alone is the old link form.
    (path ? projectsApi.openPath(path) : projectsApi.open(name)).then(setProject)
      .catch((e: any) => setError(e.message || 'not found'));
  }, [name, path]);

  if (error) {
    return (
      <div className="card">
        <strong>Project “{name}” is not available.</strong>
        <div className="muted small" style={{ marginTop: 6 }}>
          {error} · <NavLink to="/projects">Back to projects</NavLink>
        </div>
      </div>
    );
  }
  if (!project) return <div className="muted small" style={{ padding: 24 }}>Opening {name}…</div>;
  return <Chat key={project.path} project={{ name: project.name, path: project.path }} />;
}
