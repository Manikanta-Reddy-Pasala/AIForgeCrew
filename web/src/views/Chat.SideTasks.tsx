import type { SideTask } from '../api';

const LABEL: Record<SideTask['state'], string> = {
  queued: 'queued', running: 'running', done: 'done', stopped: 'stopped',
};
const CHIP: Record<SideTask['state'], string> = {
  queued: 'chip', running: 'chip info', done: 'chip ok', stopped: 'chip warn',
};

/** The side tasks of a chat: other agent runs going on beside it. Click one to
 *  watch it; Stop a running one; remove a queued or finished one. */
export function SideTasks({ tasks, viewingId, parentId, limit, onOpen, onStop, onRemove }: {
  tasks: SideTask[];
  viewingId: number | null;
  parentId: number;
  limit: number;
  onOpen: (id: number) => void;
  onStop: (id: number) => void;
  onRemove: (id: number) => void;
}) {
  if (tasks.length === 0 && viewingId === parentId) return null;
  const waiting = tasks.filter(t => t.state === 'queued').length;
  return (
    <div className="row" style={{ gap: 6, flexWrap: 'wrap', alignItems: 'center',
                                  padding: '6px 10px', borderTop: '1px solid var(--border-1)' }}>
      <span className="muted xs">Tasks</span>
      <button type="button" className={`ghost sm${viewingId === parentId ? ' active' : ''}`}
              onClick={() => onOpen(parentId)} title="The main chat"
              style={{ fontWeight: viewingId === parentId ? 600 : 400 }}>
        Main chat
      </button>
      {tasks.map(t => (
        <span key={t.id} className={CHIP[t.state]}
              title={t.state === 'done' && t.preview ? t.preview : t.prompt}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 6, maxWidth: 360,
                       outline: viewingId === t.id ? '1px solid currentColor' : undefined }}>
          <button type="button" onClick={() => onOpen(t.id)}
                  style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                           color: 'inherit', font: 'inherit', overflow: 'hidden',
                           textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 240 }}>
            {t.title || 'Side task'}
          </button>
          <span style={{ opacity: 0.75 }}>
            {LABEL[t.state]}{t.state === 'queued' && t.edits ? ' · waits for edits to finish' : ''}
          </span>
          {t.state === 'running'
            ? <button type="button" className="ghost sm" onClick={() => onStop(t.id)}
                      title="Stop this task only">■</button>
            : <button type="button" className="ghost sm" onClick={() => onRemove(t.id)}
                      title="Remove this task">✕</button>}
        </span>
      ))}
      {waiting > 0 && (
        <span className="muted xs">
          {limit <= 1
            ? 'The model serves one request at a time, so queued tasks start when the running one ends.'
            : `Up to ${limit} run at once.`}
        </span>
      )}
    </div>
  );
}
