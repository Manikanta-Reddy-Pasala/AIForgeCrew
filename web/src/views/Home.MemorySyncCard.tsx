/**
 * Memory sync settings: which admin, which group, and what the filter held back.
 *
 * Everything here comes from ONE endpoint (`/api/memory/sync/status`), which is
 * served from the record the sync cycle writes rather than probed live. A page
 * load must not be the thing that discovers the admin is down: a probe on
 * render is a twenty-second hang exactly when it matters, and the answer is
 * already on disk.
 *
 * An unreachable admin is ordinary — a laptop off the LAN, a hub being
 * rebooted — so it reads here as a stated fact with a last-synced time, not as
 * an alarm. The one state that genuinely needs the operator is
 * `needs-group-selection`: the admin serves several groups, this machine has
 * chosen none, and nothing is being sent until it does.
 */
import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { SyncStatus, SyncState } from '../api/memory';

const LABEL: Record<SyncState, string> = {
  ok: 'Syncing',
  unreachable: 'Admin unreachable',
  'needs-group-selection': 'Choose a group',
  'group-unknown': 'Group not published by this admin',
  'no-admin': 'No admin configured',
  unknown: 'Not synced yet',
};

function when(ts: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleString() : 'never';
}

function total(counts: Record<string, number>): number {
  return Object.values(counts || {}).reduce((a, b) => a + b, 0);
}

export default function MemorySyncCard() {
  const [st, setSt] = useState<SyncStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  async function load() {
    try {
      setSt(await api.syncStatus());
      setErr('');
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    load();
    // The cycle itself runs every 30 minutes; polling here is only so the panel
    // reflects a "Sync now" or a background cycle without a manual reload.
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);

  async function join(group: string) {
    if (!group) return;
    setBusy(true);
    try {
      await api.syncJoinGroup(group);
      await api.syncNow();
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function syncNow() {
    setBusy(true);
    try {
      await api.syncNow();
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (err) {
    return (
      <div className="card" style={{ marginTop: 16 }}>
        <h3>Memory sync</h3>
        <p className="muted">{err}</p>
        <button type="button" className="ghost sm" onClick={() => load()}>Retry</button>
      </div>
    );
  }
  if (!st) {
    return (
      <div className="card" style={{ marginTop: 16 }}>
        <h3>Memory sync</h3>
        <p className="muted">Loading…</p>
      </div>
    );
  }

  const held = total(st.blocked);
  const isAdmin = st.role === 'admin';

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h3>Memory sync</h3>

      <p className="muted" style={{ marginTop: 0 }}>
        {isAdmin
          ? 'This machine is the admin: it merges what every other machine sends and serves the result back.'
          : `${LABEL[st.state]} — ${st.admin || 'no admin url set'}`}
      </p>

      {/* Not an alarm: an admin that is down is the ordinary state of a laptop
          away from the LAN, and the useful fact is when it last worked. */}
      {!isAdmin && !st.reachable && st.state !== 'no-admin' && (
        <p className="muted">
          Last synced {when(st.last_ok)}
          {st.last_error ? ` — ${st.last_error}` : ''}
        </p>
      )}

      {!isAdmin && st.groups_available.length > 1 && (
        <label style={{ display: 'block', marginBottom: 8 }}>
          Group{' '}
          <select value={st.group} disabled={busy}
                  onChange={(e) => join(e.target.value)}>
            <option value="">— select a group —</option>
            {st.groups_available.map(g => <option key={g} value={g}>{g}</option>)}
          </select>
          {st.state === 'needs-group-selection' && (
            <span className="muted" style={{ marginLeft: 8 }}>
              nothing is sent until a group is chosen
            </span>
          )}
        </label>
      )}
      {!isAdmin && st.groups_available.length <= 1 && (
        <p className="muted">Group: {st.group || 'ungrouped'}</p>
      )}

      {!isAdmin && (
        <div className="row" style={{ gap: 24, marginBottom: 8 }}>
          <span>Waiting to send: <strong>{st.pending}</strong></span>
          <span>Sent in total: <strong>{st.pushed_total}</strong></span>
          <span>Held back: <strong>{held}</strong></span>
        </div>
      )}

      {st.recent_blocks.length > 0 && (
        <details>
          <summary>What was held back, and why</summary>
          <ul>
            {st.recent_blocks.map(b => (
              <li key={`${b.key}-${b.at}`}>
                <code>{b.key}</code> — {b.reason}{' '}
                <span className="muted">({b.rule})</span>
              </li>
            ))}
          </ul>
          <p className="muted">
            Notes are held back whole, never edited: a note that mentions a
            credential usually identifies the system too. The rule name is
            recorded; the note's text never leaves this machine.
          </p>
        </details>
      )}

      {!isAdmin && (
        <button type="button" onClick={syncNow}
                disabled={busy || st.state === 'needs-group-selection'
                          || st.state === 'no-admin'}>
          {busy ? 'Syncing…' : 'Sync now'}
        </button>
      )}
    </div>
  );
}
