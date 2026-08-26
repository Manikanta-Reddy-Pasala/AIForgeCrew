/**
 * Memory sync settings: which admin, which group, and what the filter held back.
 *
 * Everything here comes from ONE endpoint (`/api/memory/sync/status`), which is
 * served from the record the sync cycle writes rather than probed live. A page
 * load must not be the thing that discovers the admin is down: a probe on
 * render is a twenty-second hang exactly when it matters, and the answer is
 * already on disk.
 *
 * Two fields are editable, and both are the operator saying something the
 * machine cannot work out for itself:
 *
 *  - **the admin** — host and port. Saving it makes this machine a spoke, and
 *    runs one cycle immediately so the group list appears without waiting for
 *    the next tick.
 *  - **the group** — filled from the admin's own reply. Nobody has to type a
 *    group name here; if nothing is chosen the admin's first group is taken and
 *    the panel says so, because a machine that quietly syncs nothing looks
 *    exactly like one that is syncing fine.
 *
 * Either field is read-only when it is pinned in `.env`, since an edit would be
 * silently ignored — showing an editable box that does nothing is worse than
 * showing why it cannot be edited.
 */
import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { SyncStatus, SyncState } from '../api/memory';

const LABEL: Record<SyncState, string> = {
  ok: 'Syncing',
  'group-defaulted': 'Syncing (default group)',
  unreachable: 'Admin unreachable',
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
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  async function load() {
    try {
      const next = await api.syncStatus();
      setSt(next);
      // Only seed the input while the operator is not mid-edit, or the 30s
      // refresh below would wipe what they are typing.
      setDraft(d => (d === null ? next.admin : d));
      setErr('');
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);

  async function run<T>(fn: () => Promise<T>) {
    setBusy(true);
    try {
      await fn();
      await load();
      setErr('');
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const saveAdmin = () => run(async () => {
    await api.syncSetAdmin((draft ?? '').trim());
    setDraft(null);                       // re-seed from whatever took effect
  });
  const join = (group: string) => run(() => api.syncJoinGroup(group));
  const syncNow = () => run(() => api.syncNow());

  if (!st) {
    return (
      <div className="card" style={{ marginTop: 16 }}>
        <h3>Memory sync</h3>
        <p className="muted">{err || 'Loading…'}</p>
        {err && <button type="button" className="ghost sm" onClick={load}>Retry</button>}
      </div>
    );
  }

  const held = total(st.blocked);
  const isAdmin = st.role === 'admin';
  const dirty = draft !== null && draft.trim() !== st.admin;

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h3>Memory sync</h3>

      <p className="muted" style={{ marginTop: 0 }}>
        {isAdmin
          ? 'This machine is the admin: it merges what every other machine sends and serves the result back. Leave the address below empty to keep it that way.'
          : LABEL[st.state]}
      </p>

      {/* ── the admin: host and port ─────────────────────────────── */}
      <label style={{ display: 'block', marginBottom: 10 }}>
        <div style={{ marginBottom: 4 }}>Admin address</div>
        <input
          type="text"
          value={draft ?? ''}
          placeholder="http://192.168.1.20:8799"
          disabled={busy || st.admin_pinned}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && dirty) saveAdmin(); }}
          style={{ width: 320, maxWidth: '100%' }}
        />
        <button type="button" onClick={saveAdmin}
                disabled={busy || st.admin_pinned || !dirty}
                style={{ marginLeft: 8 }}>
          Save
        </button>
        {st.admin_pinned && (
          <div className="muted">
            pinned by AIFORGE_ADMIN_URL in .env — edit it there
          </div>
        )}
      </label>

      {/* ── the group: whatever the admin replies with ───────────── */}
      {!isAdmin && (
        <label style={{ display: 'block', marginBottom: 10 }}>
          <div style={{ marginBottom: 4 }}>Group</div>
          {st.groups_available.length > 0 ? (
            <select value={st.group} disabled={busy || st.group_pinned}
                    onChange={(e) => join(e.target.value)}>
              {st.groups_available.map(g => <option key={g} value={g}>{g}</option>)}
            </select>
          ) : (
            <span className="muted">
              {st.admin ? 'this admin publishes no groups (ungrouped)'
                        : 'set an admin address first'}
            </span>
          )}
          {st.state === 'group-defaulted' && (
            <div className="muted">
              nothing was chosen, so the admin's first group was used — change it
              here if that is the wrong pool
            </div>
          )}
          {st.state === 'group-unknown' && (
            <div className="muted">
              this admin no longer publishes <code>{st.group}</code>; the choice
              was kept rather than silently moved
            </div>
          )}
          {st.group_pinned && (
            <div className="muted">pinned by AIFORGE_SYNC_GROUP in .env</div>
          )}
        </label>
      )}

      {/* An admin that is down is the ordinary state of a laptop away from the
          LAN, so it reads as a fact with a last-synced time, not an alarm. */}
      {!isAdmin && !st.reachable && st.state !== 'no-admin' && (
        <p className="muted">
          Last synced {when(st.last_ok)}{st.last_error ? ` — ${st.last_error}` : ''}
        </p>
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
              <li key={`${b.key}-${b.rule}`}>
                <code>{b.key}</code> — {b.reason}{' '}
                <span className="muted">({b.rule})</span>
              </li>
            ))}
          </ul>
          <p className="muted">
            Notes are held back whole, never edited: a note that mentions a
            credential usually identifies the system too. The rule is recorded;
            the note's text never leaves this machine.
          </p>
        </details>
      )}

      {err && <p className="muted">{err}</p>}

      {!isAdmin && (
        <button type="button" onClick={syncNow}
                disabled={busy || st.state === 'no-admin'}>
          {busy ? 'Working…' : 'Sync now'}
        </button>
      )}
    </div>
  );
}
