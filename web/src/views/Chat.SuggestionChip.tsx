/**
 * The predicted next step, under the reply.
 *
 * Two shapes, and BOTH are shown. An ACT is reported in the past tense — it has
 * already happened — because a chip that appeared only when there was a
 * question would teach the user that no chip means nothing was done, which is
 * exactly the wrong lesson.
 *
 * "Do it" sends the action back as an ordinary chat message rather than
 * executing anything from here. That keeps every approval gate, tool policy and
 * transcript entry on one path; a second execution path that skipped them is
 * the hole this feature must not open.
 *
 * Dismissing is recorded as carefully as accepting: a feature that learns only
 * from its wins drifts, and "wrong about this user" is the more useful signal.
 */
import { useState } from 'react';
import { api } from '../api/client';
import type { Suggestion } from '../api/chat';

export default function SuggestionChip(
  { s, onSend }: { s: Suggestion; onSend: (text: string) => void },
) {
  const [gone, setGone] = useState(false);
  if (gone) return null;

  const acted = s.verdict === 'ACT';

  async function answer(accepted: boolean) {
    setGone(true);
    try {
      await api.suggestionOutcome(s.id, accepted);
    } catch {
      /* recording an outcome must never break the chat */
    }
    if (accepted && !acted) onSend(s.action);
  }

  return (
    <div className="card" style={{ marginTop: 8, padding: '8px 12px' }}>
      <span className="muted">{acted ? 'Also did' : 'Next'}: </span>
      <span>{s.action}</span>
      {s.rationale && <span className="muted"> — {s.rationale}</span>}
      <span style={{ marginLeft: 12, whiteSpace: 'nowrap' }}>
        {!acted && (
          <button type="button" className="sm" onClick={() => answer(true)}>
            Do it
          </button>
        )}
        <button type="button" className="ghost sm" style={{ marginLeft: 6 }}
                onClick={() => answer(false)}>
          {acted ? 'Not useful' : 'Dismiss'}
        </button>
      </span>
    </div>
  );
}
