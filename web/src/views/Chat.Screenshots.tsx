import { useState, type CSSProperties } from 'react';
import { chatCaptureURL } from '../api';
import { AgentStep } from './Chat.types';

// The id visual.save_capture makes: label-<ms>-<seq>-<hex>.
const CAPTURE_ID = /^[A-Za-z0-9_-]+-\d+-\d+-[0-9a-f]+$/;

// Tools that TAKE a screenshot. ui_ask only re-reads one (maybe from an
// earlier turn, maybe an id the model made up), so it never adds an image.
const TAKES_SCREENSHOTS = new Set(['ui_check', 'browse']);

// Screenshots the turn's tools took, oldest first, each once. Read from the
// tool results so a reloaded chat shows them again.
export function captureIds(steps: AgentStep[]): string[] {
  const out: string[] = [];
  for (const s of steps) {
    if (s.kind !== 'tool' || s.pending || !TAKES_SCREENSHOTS.has(s.name)) continue;
    const res = s.result as { ok?: unknown; capture_id?: unknown } | undefined;
    if (res?.ok === false) continue;
    const id = res?.capture_id;
    if (typeof id === 'string' && CAPTURE_ID.test(id) && !out.includes(id)) out.push(id);
  }
  return out;
}

// A screenshot is a deliverable like the Changes diff: shown under the answer,
// not folded away with the steps. The LAST one is the screen the agent ended
// on, so it is shown full width; earlier attempts (a wrong route, a 404) stay
// as small thumbnails. Click opens the full-size image.
// Captures are pruned (the newest 300 are kept), so an old chat can point at
// one that is gone: say so instead of showing a broken image.
function Shot({ id, style }: Readonly<{ id: string; style: CSSProperties }>) {
  const [gone, setGone] = useState(false);
  if (gone) {
    return <span className="xs muted" title={id}>screenshot expired</span>;
  }
  return (
    <a href={chatCaptureURL(id)} target="_blank" rel="noreferrer"
       title="Open the full-size screenshot" style={{ display: 'block', maxWidth: 720 }}>
      <img src={chatCaptureURL(id)} alt={`screenshot ${id}`} loading="lazy"
           onError={() => setGone(true)} style={style} />
    </a>
  );
}

export function Screenshots({ ids }: Readonly<{ ids: string[] }>) {
  if (!ids.length) return null;
  const last = ids[ids.length - 1];
  const earlier = ids.slice(0, -1);
  const frame: CSSProperties = { border: '1px solid var(--border-1)', borderRadius: 'var(--r-sm)', display: 'block' };
  return (
    <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 8 }}>
      <Shot id={last} style={{ ...frame, width: '100%', height: 'auto' }} />
      {earlier.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          <span className="xs muted">Earlier in this turn:</span>
          {earlier.map(id => (
            <Shot key={id} id={id}
                  style={{ ...frame, width: 120, height: 75, objectFit: 'cover', objectPosition: 'top' }} />
          ))}
        </div>
      )}
    </div>
  );
}
