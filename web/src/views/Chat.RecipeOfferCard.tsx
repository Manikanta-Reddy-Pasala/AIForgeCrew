/**
 * A recipe offer, under the reply: the harness saw the agent run a command of
 * a known family (test, build, lint…) that this project has no recipe for, and
 * asks whether to keep it. Zero or more arrive per turn (`recipe_offer`
 * events after `done`), one card each.
 *
 * Both answers are recorded: "Save" stores the command as the project's recipe
 * for that verb; "Not this" declines the verb for this project so the same
 * offer does not come back every turn. Either way the card goes — a card that
 * stayed after a click would read as "it did not take".
 */
import { useState } from 'react';
import { toast } from 'sonner';
import { chatApi } from '../api/chat';

export type RecipeOffer = { verb: string; cmd: string; cwd: string };

export default function RecipeOfferCard(
  { sessionId, offer, onDone }: Readonly<{ sessionId: number; offer: RecipeOffer; onDone: () => void }>,
) {
  const [busy, setBusy] = useState(false);

  async function act(save: boolean) {
    if (busy) return;
    setBusy(true);
    try {
      if (save) {
        await chatApi.saveRecipe(sessionId, { verb: offer.verb, cmd: offer.cmd, cwd: offer.cwd });
        toast.success(`Saved as the project's ${offer.verb} recipe`);
      } else {
        await chatApi.declineRecipe(sessionId, offer.verb);
      }
      onDone();
    } catch (e: any) {
      toast.error(e?.message || (save ? 'Could not save the recipe' : 'Could not decline'));
    } finally {
      setBusy(false);
    }
  }

  // The offer's cwd is relative to the project; "." is the root and not worth saying.
  const inSubdir = !!offer.cwd && offer.cwd !== '.';
  return (
    <div className="card" role="group" aria-label={`recipe offer: ${offer.verb}`}
         style={{ marginTop: 8, padding: '8px 12px' }}>
      <span className="muted">Recipe: </span>
      <span>
        Save <code>{offer.cmd}</code>
        {inSubdir && <span className="muted"> (in <code>{offer.cwd}</code>)</span>}
        {' '}as this project's <b>{offer.verb}</b> recipe?
      </span>
      <span style={{ marginLeft: 12, whiteSpace: 'nowrap' }}>
        <button type="button" className="sm" disabled={busy} onClick={() => act(true)}>
          Save
        </button>
        <button type="button" className="ghost sm" style={{ marginLeft: 6 }} disabled={busy}
                onClick={() => act(false)}>
          Not this
        </button>
      </span>
    </div>
  );
}
