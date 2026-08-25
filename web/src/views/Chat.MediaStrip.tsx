import { ChatMedia, chatMediaRawURL } from '../api';
import { Icon } from '../icons';

// Thumbnail strip of images attached to the session. Each shows the image, an
// editable description (what makes it queryable when the model can't see it),
// and a delete. A note clarifies whether the model can actually see images.
export function MediaStrip({ media, vision, onDescribe, onDelete }: Readonly<{
  media: ChatMedia[];
  vision: boolean;
  onDescribe: (id: number, d: string) => void;
  onDelete: (id: number) => void;
}>) {
  if (!media.length) return null;
  const imgN = media.filter(m => (m.mime || '').startsWith('image/')).length;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '6px 0' }}>
      <div className="xs muted">
        {media.length} file{media.length === 1 ? '' : 's'} in this session
        {imgN > 0 && <> · {vision ? 'model can see images' : 'image descriptions only (model not vision-capable)'}</>}
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {media.map(m => {
          const isImg = (m.mime || '').startsWith('image/');
          return (
          <div key={m.id} style={{ display: 'flex', gap: 6, alignItems: 'flex-start',
                                    border: '1px solid var(--border-1)', borderRadius: 6, padding: 6, maxWidth: 320 }}>
            <a href={chatMediaRawURL(m.id)} target="_blank" rel="noreferrer"
               style={{ textDecoration: 'none' }}>
              {isImg ? (
                <img src={chatMediaRawURL(m.id)} alt={m.filename}
                     style={{ width: 48, height: 48, objectFit: 'cover', borderRadius: 4 }} />
              ) : (
                <div style={{ width: 48, height: 48, display: 'flex', alignItems: 'center',
                              justifyContent: 'center', borderRadius: 4,
                              background: 'var(--bg-2)', color: 'var(--fg-1)' }}>
                  <Icon.File size={22} />
                </div>
              )}
            </a>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div className="xs" style={{ fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.filename}</div>
              {isImg ? (
                <textarea
                  key={`${m.id}:${m.description || ''}`}
                  defaultValue={m.description}
                  placeholder={vision ? 'description (auto/edit)…' : 'describe this image so it can be asked about…'}
                  onBlur={e => { if (e.target.value !== m.description) onDescribe(m.id, e.target.value); }}
                  rows={2}
                  style={{ width: '100%', fontSize: 11, resize: 'vertical', marginTop: 2 }}
                />
              ) : (
                <div className="xs muted" style={{ marginTop: 2 }}>
                  {m.description ? 'text extracted — queryable' : 'no readable text extracted'}
                </div>
              )}
            </div>
            <button type="button" className="ghost xs" title="Remove"
                    style={{ padding: '0 4px', cursor: 'pointer' }}
                    onClick={() => onDelete(m.id)}>✕</button>
          </div>
          );
        })}
      </div>
    </div>
  );
}
