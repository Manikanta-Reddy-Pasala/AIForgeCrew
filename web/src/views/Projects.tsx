import { useEffect, useState } from 'react';
import { NavLink, useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';
import { projectsApi, type Project, type ProjectList } from '../api';
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

  const q = filter.trim().toLowerCase();
  const shown = (data?.projects || []).filter(p => !q || p.name.toLowerCase().includes(q));

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
        <input placeholder="filter projects…" value={filter}
               onChange={e => setFilter(e.target.value)} style={{ minWidth: 220 }} />
      </div>

      {data === null && <div className="muted small">Loading…</div>}

      {data !== null && data.projects.length === 0 && (
        <div className="card">
          <strong>No folders found{data.root ? ` under ${data.root}` : ''}.</strong>
          <div className="muted small" style={{ marginTop: 6 }}>
            {data.exists
              ? 'Put a repo folder there and it will show up here.'
              : 'That folder is not available to AIForge. Mount your repos folder '
                + '(run with --repos DIR) or set the repos base folder.'}
            {' '}<NavLink to="/">Open settings</NavLink>
          </div>
        </div>
      )}

      <div style={{ display: 'grid', gap: 12,
                    gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))' }}>
        {shown.map(p => (
          <button type="button" key={p.name} className="card"
                  onClick={() => navigate(`/projects/${encodeURIComponent(p.name)}`)}
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
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    setProject(null);
    setError('');
    projectsApi.open(name).then(setProject)
      .catch((e: any) => setError(e.message || 'not found'));
  }, [name]);

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
  return <Chat key={project.name} project={{ name: project.name, path: project.path }} />;
}
