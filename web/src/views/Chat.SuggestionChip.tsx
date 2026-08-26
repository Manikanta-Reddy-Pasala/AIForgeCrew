/**
 * The predicted next step, under the reply.
 *
 * Two shapes, and BOTH are shown. An OFFER waits for a click; an ACT is safe,
 * reversible and confident enough to run on its own, so it sends itself the
 * moment it appears — and SAYS SO. A chip that appeared only when there was a
 * question would teach the user that no chip means nothing was done, which is
 * exactly the wrong lesson.
 *
 * BOTH shapes send the action back as an ordinary chat message rather than
 * executing anything from here. That keeps every approval gate, tool policy and
 * transcript entry on one path; a second execution path that skipped them is
 * the hole this feature must not open. It is also why an ACT is auto-SENT
 * rather than auto-RUN: the difference is who had to click, never which gates
 * applied.
 *
 * A turn the user did not start cannot auto-act — ``Chat.tsx`` downgrades the
 * next suggestion to an OFFER — so this can never chain into a runaway.
 *
 * Dismissing is recorded as carefully as accepting: a feature that learns only
 * from its wins drifts, and "wrong about this user" is the more useful signal.
 */
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { Suggestion } from '../api/chat';

export default function SuggestionChip(
  { s, onSend }: Readonly<{ s: Suggestion; onSend: (text: string) => void }>,
) {
  const [gone, setGone] = useState(false);
  const fired = useRef(false);
  const acted = s.verdict === 'ACT';

  async function answer(accepted: boolean) {
    setGone(true);
    try {
      await api.suggestionOutcome(s.id, accepted);
    } catch {
      /* recording an outcome must never break the chat */
    }
    if (accepted) onSend(s.action);
  }

  // An ACT runs without being asked — that is what "act when it is safe" means.
  // Guarded by a ref rather than the `gone` state because React may mount an
  // effect twice in development, and sending the same action twice is exactly
  // the failure an auto-action must not have.
  useEffect(() => {
    if (!acted || fired.current) return;
    fired.current = true;
    answer(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [acted, s.id]);

  if (gone) return null;

  return (
    <div className="card" style={{ marginTop: 8, padding: '8px 12px' }}>
      <span className="muted">{acted ? 'Running' : 'Next'}: </span>
      <span>{s.action}</span>
      {s.rationale && <span className="muted"> — {s.rationale}</span>}
      <span style={{ marginLeft: 12, whiteSpace: 'nowrap' }}>
        {!acted && (
          <>
            <button type="button" className="sm" onClick={() => answer(true)}>
              Do it
            </button>
            <button type="button" className="ghost sm" style={{ marginLeft: 6 }}
                    onClick={() => answer(false)}>
              Dismiss
            </button>
          </>
        )}
      </span>
    </div>
  );
}
