import { useState } from 'react';
import { toast } from 'sonner';
import { api } from '../api';
import { Icon } from '../icons';
import { DropZone, readAsBase64, MAX_FILE_BYTES, formatBytes } from '../components/FileUpload';
import type { AttachedFile, NewFile } from './TicketDetail.types';
import { IMAGE_EXT, fmtSize } from './TicketDetail.helpers';

// Attachments grid with inline add/remove. Removals + new uploads are
// staged locally and applied in one PATCH on Save.
export function AttachmentsBlock({
  identifier, files, onSaved,
}: { identifier: string; files?: AttachedFile[]; onSaved: () => void }) {
  const existing = Array.isArray(files) ? files : [];
  const [editing, setEditing] = useState(false);
  const [removed, setRemoved] = useState<Set<string>>(new Set());
  const [added, setAdded] = useState<NewFile[]>([]);
  const [saving, setSaving] = useState(false);

  function reset() { setRemoved(new Set()); setAdded([]); setEditing(false); }

  function toggleRemove(name: string) {
    setRemoved(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  }

  async function onFiles(picked: File[]) {
    for (const f of picked) {
      if (f.size > MAX_FILE_BYTES) {
        toast.error(`${f.name} is ${formatBytes(f.size)} — over 5MB cap, skipping`);
        continue;
      }
      const content_b64 = await readAsBase64(f);
      setAdded(prev => [...prev.filter(x => x.name !== f.name),
        { name: f.name, size: f.size, content_b64 }]);
    }
  }

  async function save() {
    setSaving(true);
    try {
      await api.patch(identifier, {
        attached_files: added,
        remove_files: Array.from(removed),
      });
      toast.success('Attachments updated');
      reset();
      onSaved();
    } catch (e: any) { toast.error(e.message); }
    finally { setSaving(false); }
  }

  const dirty = removed.size > 0 || added.length > 0;
  if (existing.length === 0 && !editing) {
    return (
      <div className="card">
        <div className="card-header" style={{ display: 'flex', alignItems: 'center' }}>
          <h2 style={{ flex: 1 }}>Attachments</h2>
          <button className="ghost sm" onClick={() => setEditing(true)}>
            <Icon.Edit size={14} /> Add
          </button>
        </div>
        <div className="muted small">No attachments.</div>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="card-header" style={{ display: 'flex', alignItems: 'center' }}>
        <h2 style={{ flex: 1 }}>Attachments ({existing.length})</h2>
        {!editing && (
          <button className="ghost sm" onClick={() => setEditing(true)}>
            <Icon.Edit size={14} /> Edit
          </button>
        )}
      </div>
      <div className="stack" style={{ gap: 12 }}>
        {existing.map((f, i) => {
          const url = `/files/${encodeURIComponent(identifier)}/${encodeURIComponent(f.name)}`;
          const isImage = IMAGE_EXT.test(f.name);
          const isRemoved = removed.has(f.name);
          return (
            <div key={i} className="row"
              style={{ gap: 12, alignItems: 'flex-start', opacity: isRemoved ? 0.4 : 1 }}>
              {isImage ? (
                <a href={url} target="_blank" rel="noopener">
                  <img src={url} alt={f.name}
                    style={{
                      maxWidth: 160, maxHeight: 120,
                      border: '1px solid var(--border-1)', borderRadius: 4,
                      objectFit: 'cover',
                    }} />
                </a>
              ) : (
                <div style={{
                  width: 64, height: 64, display: 'flex',
                  alignItems: 'center', justifyContent: 'center',
                  border: '1px solid var(--border-1)', borderRadius: 4,
                  fontSize: 12, color: 'var(--muted)',
                }}>
                  {(f.name.split('.').pop() || 'file').slice(0, 4).toUpperCase()}
                </div>
              )}
              <div className="stack" style={{ gap: 4, minWidth: 0, flex: 1 }}>
                <a href={url} target="_blank" rel="noopener"
                  style={{ wordBreak: 'break-all', textDecoration: isRemoved ? 'line-through' : undefined }}>
                  {f.name}
                </a>
                <div className="muted small">{fmtSize(f.size)}</div>
                {f.path && (
                  <code className="mono small muted" style={{ wordBreak: 'break-all' }}>
                    {f.path}
                  </code>
                )}
              </div>
              {editing && (
                <button className="ghost sm danger" onClick={() => toggleRemove(f.name)}
                  title={isRemoved ? 'Keep' : 'Remove'}>
                  {isRemoved ? 'Undo' : <Icon.Trash size={14} />}
                </button>
              )}
            </div>
          );
        })}
      </div>
      {editing && (
        <div className="stack" style={{ gap: 8, marginTop: 12 }}>
          <DropZone onFiles={onFiles} />
          {added.length > 0 && (
            <div className="stack" style={{ gap: 2 }}>
              {added.map((f, i) => (
                <div key={i} className="small muted">
                  📎 {f.name} ({formatBytes(f.size)}){' '}
                  <button className="ghost sm danger"
                    onClick={() => setAdded(prev => prev.filter(x => x.name !== f.name))}>
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="row" style={{ gap: 8, justifyContent: 'flex-end' }}>
            <button className="ghost sm" onClick={reset} disabled={saving}>Cancel</button>
            <button onClick={save} disabled={saving || !dirty}>
              <Icon.Send size={14} /> {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
