/**
 * Sandbox folders: which host folders the docker-mode box can see.
 *
 * The box sees ~/.aiforge and nothing else of the host, plus a projects folder
 * (`./run.sh --repos DIR`) and any folder added here. A running container
 * cannot mount into itself — the host's `./run.sh` mounts the list when it
 * (re)starts the box — so a folder added here shows "waiting" until then, and
 * the card says so instead of pretending it is already visible.
 */
import { useEffect, useState } from 'react';
import { j } from '../api/core';

type Folder = { path: string; kind: 'config' | 'projects' | 'folder'; status: string };
type Mounts = { sandbox: boolean; folders: Folder[]; restart_needed: boolean; file: string };

const KIND_LABEL: Record<Folder['kind'], string> = {
  config: 'AIForge folder — settings, credentials, memory',
  projects: 'projects folder (--repos)',
  folder: 'added folder',
};

export default function MountsCard() {
  const [data, setData] = useState<Mounts | null>(null);
  const [path, setPath] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  const load = () => j<Mounts>('/runtime/mounts').then(setData).catch(e => setMsg(String(e)));
  useEffect(() => { load(); }, []);

  async function change(method: 'POST' | 'DELETE', p: string) {
    setBusy(true);
    setMsg('');
    try {
      const d = method === 'POST'
        ? await j<Mounts>('/runtime/mounts', { method, headers: { 'Content-Type': 'application/json' },
                                             body: JSON.stringify({ path: p }) })
        : await j<Mounts>(`/runtime/mounts?path=${encodeURIComponent(p)}`, { method });
      setData(d);
      if (method === 'POST') setPath('');
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <div className="card">Sandbox folders — {msg || 'loading…'}</div>;

  return (
    <div className="card">
      <h3>Sandbox folders</h3>
      {data.sandbox ? (
        <p style={{ fontSize: 13, opacity: 0.8 }}>
          AIForge runs in an Ubuntu 24.04 box with full rights inside it. From this machine it
          sees only the folders below, each at the same path. Add a folder to give the agent
          access to it; it is mounted the next time you start the box with <code>./run.sh</code> on
          the host.
        </p>
      ) : (
        <p style={{ fontSize: 13, opacity: 0.8 }}>
          Native mode (<code>./run.sh --native</code>): AIForge runs directly on this machine, so
          nothing is mounted — the list below applies when you run it in the sandbox.
        </p>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 10 }}>
        {data.folders.map(f => (
          <div key={f.path} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
            <code style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }} title={f.path}>{f.path}</code>
            <span className="muted xs">{KIND_LABEL[f.kind]}</span>
            <span className="xs" style={{ color: f.status === 'mounted' ? 'var(--ok, #2faa66)' : 'var(--warn, #dd9b3c)' }}>
              {f.status}
            </span>
            {f.kind === 'folder' && !f.status.startsWith('removed') && (
              <button type="button" className="ghost xs" disabled={busy}
                      onClick={() => change('DELETE', f.path)} title="Stop mounting this folder">Remove</button>
            )}
          </div>
        ))}
        {data.folders.length === 0 && <div className="muted xs">No folders reported.</div>}
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <input value={path} onChange={e => setPath(e.target.value)} spellCheck={false}
               placeholder="/home/me/code/my-project" style={{ flex: 1, minWidth: 220, fontFamily: 'monospace' }}
               onKeyDown={e => { if (e.key === 'Enter' && path.trim()) change('POST', path.trim()); }} />
        <button type="button" disabled={busy || !path.trim()} onClick={() => change('POST', path.trim())}>
          Mount folder
        </button>
      </div>
      {data.restart_needed && (
        <div className="xs" style={{ marginTop: 8, color: 'var(--warn, #dd9b3c)' }}>
          Restart the box on the host to apply: <code>./run.sh</code>
        </div>
      )}
      {msg && <div className="xs" style={{ marginTop: 6, color: 'var(--err)' }}>{msg}</div>}
    </div>
  );
}
