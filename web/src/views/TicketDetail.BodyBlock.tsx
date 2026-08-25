import { useState } from 'react';
import { toast } from 'sonner';
import { api } from '../api';
import { Icon } from '../icons';

// Editable ticket body (description). Read-only <pre> until Edit; then a
// textarea + Save/Cancel that PATCHes the body.
export function BodyBlock({
  identifier, body, onSaved,
}: Readonly<{ identifier: string; body?: string; onSaved: () => void }>) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(body || '');
  const [saving, setSaving] = useState(false);

  function startEdit() { setDraft(body || ''); setEditing(true); }

  async function save() {
    setSaving(true);
    try {
      await api.patch(identifier, { body: draft });
      toast.success('Description updated');
      setEditing(false);
      onSaved();
    } catch (e: any) { toast.error(e.message); }
    finally { setSaving(false); }
  }

  const bodyView = body
    ? <pre style={{ whiteSpace: 'pre-wrap' }}>{body}</pre>
    : <div className="muted small">(empty)</div>;

  return (
    <div className="card">
      <div className="card-header" style={{ display: 'flex', alignItems: 'center' }}>
        <h2 style={{ flex: 1 }}>Body</h2>
        {!editing && (
          <button type="button" className="ghost sm" onClick={startEdit}>
            <Icon.Edit size={14} /> Edit
          </button>
        )}
      </div>
      {editing ? (
        <div className="stack" style={{ gap: 8 }}>
          <textarea
            rows={10}
            value={draft}
            onChange={e => setDraft(e.target.value)}
            style={{ width: '100%', fontFamily: 'inherit' }}
          />
          <div className="row" style={{ gap: 8, justifyContent: 'flex-end' }}>
            <button type="button" className="ghost sm" onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </button>
            <button type="button" onClick={save} disabled={saving}>
              <Icon.Send size={14} /> {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      ) : (
        bodyView
      )}
    </div>
  );
}
