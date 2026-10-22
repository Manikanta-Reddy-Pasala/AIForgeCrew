/**
 * The scope net, under the reply: files the agent changed that no requested
 * board item claimed (`scope_net` event after `done`, at most one per turn).
 *
 * The one action is a revert of exactly those paths to how they were when the
 * turn started (`head0`; null when the turn began unversioned). The server
 * does the revert and says which paths it refused — a path edited again since,
 * or one it cannot restore, stays as it is and is named. Nothing is reverted
 * from here, and nothing else is: a wider "undo the turn" is the checkpoint's
 * job, not this card's.
 */
import { useState } from 'react';
import { toast } from 'sonner';
import { chatApi } from '../api/chat';

export type ScopeNet = { paths: string[]; head0: string | null };

export default function ScopeNetCard(
  { sessionId, net, onDone }: Readonly<{ sessionId: number; net: ScopeNet; onDone: (msg: string) => void }>,
) {
  const [busy, setBusy] = useState(false);

  async function revert() {
    if (busy) return;
    setBusy(true);
    try {
      const r = await chatApi.revertPaths(sessionId, net.paths, net.head0);
      const reverted = r.reverted ?? [];
      const refused = r.refused ?? [];
      // The refused list is always said, whatever else happened.
      if (refused.length) toast.warning(`Not reverted: ${refused.join(', ')}`);
      if (!reverted.length) {
        // Nothing moved (whatever `ok` says): the card stays so the user can
        // retry or keep — a card that vanished here would read as "reverted".
        if (!refused.length) toast.error('Nothing was reverted');
        return;
      }
      onDone(`Reverted ${reverted.length} file(s)`);
    } catch (e: any) {
      toast.error(e?.message || 'Could not revert');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" role="group" aria-label="unrequested changes"
         style={{ marginTop: 8, padding: '8px 12px', borderColor: 'var(--warn, #d4a72c)' }}>
      <span style={{ color: 'var(--warn, #d4a72c)', fontWeight: 600 }}>⚠ Unrequested changes: </span>
      <span style={{ display: 'inline-flex', flexWrap: 'wrap', gap: 6 }}>
        {net.paths.map(p => <code key={p} title={p}>{p}</code>)}
      </span>
      <span style={{ marginLeft: 12, whiteSpace: 'nowrap' }}>
        <button type="button" className="sm" disabled={busy} onClick={revert}
                aria-label={`Revert ${net.paths.length} unrequested file change(s)`}>
          Revert these
        </button>
        <button type="button" className="ghost sm" style={{ marginLeft: 6 }} disabled={busy}
                onClick={() => onDone('')} aria-label="Keep the unrequested changes">
          Keep
        </button>
      </span>
    </div>
  );
}
