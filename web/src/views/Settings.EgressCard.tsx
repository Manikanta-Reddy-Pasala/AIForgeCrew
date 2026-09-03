/**
 * Egress allowlist: the hosts this machine may talk to.
 *
 * Enforcement is always on and the list DEFAULTS TO DENY, which only works
 * because most of it is derived rather than typed. The configured integrations,
 * the model endpoint and the observability sink are already allowed; this card
 * shows them read-only so nobody re-adds their Jira host by hand and is then
 * surprised that deleting it changes nothing — the derived entry follows the
 * integration config, and the way to remove it is to unconfigure the
 * integration.
 *
 * This machine and the LAN are deliberately absent. They are not egress, they
 * need no entry, and listing them would invite someone to "tidy up" the list
 * and lose their own dev server.
 */
import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { EgressHosts } from '../api/client';

function Chips({ hosts, note }: { hosts: string[]; note: string }) {
  if (!hosts.length) return null;
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 12, opacity: 0.7, marginBottom: 4 }}>{note}</div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {hosts.map(h => (
          <span key={h} style={{
            fontSize: 12, padding: '2px 8px', borderRadius: 12,
            border: '1px solid var(--border, #ccc)', opacity: 0.85,
          }}>{h}</span>
        ))}
      </div>
    </div>
  );
}

export default function EgressCard() {
  const [data, setData] = useState<EgressHosts | null>(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    api.egressHosts()
      .then(d => { setData(d); setDraft(d.extra_hosts.join('\n')); })
      .catch(e => setMsg(`could not load: ${e}`));
  }, []);

  async function save() {
    setBusy(true);
    setMsg('');
    try {
      const hosts = draft.split('\n').map(s => s.trim()).filter(Boolean);
      const d = await api.setEgressHosts(hosts);
      setData(d);
      // Show what was STORED, not what was typed: the server normalises a
      // pasted URL down to its host and drops duplicates, and a box that keeps
      // showing the URL you typed hides that.
      setDraft(d.extra_hosts.join('\n'));
      setMsg(`saved — ${d.effective.length} host(s) allowed`);
    } catch (e) {
      setMsg(`save failed: ${e}`);
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <div className="card">Egress — {msg || 'loading…'}</div>;

  return (
    <div className="card">
      <h3>Egress allowlist</h3>
      <p style={{ fontSize: 13, opacity: 0.8 }}>
        This machine only talks to the hosts below. Everything else is refused,
        including web pages — there is no web search on this install. Your own
        machine and LAN are always reachable and need no entry. Anything you add
        here can be READ but never written to.
      </p>

      <Chips hosts={data.derived}
             note="Allowed automatically, and the only hosts that may RECEIVE data — your configured integrations and model endpoint" />
      <Chips hosts={data.env}
             note="From AIFORGE_EGRESS_ALLOW_HOSTS (.env — edit there, not here)" />

      <label style={{ fontSize: 12, opacity: 0.7 }}>
        Additional hosts to READ, one per line (a URL or a bare host both work).
        These are fetch-only: the agent can read pages there, but cannot post,
        upload or send anything to them. A destination that needs to receive
        data has to be set up as an integration.
      </label>
      <textarea
        value={draft}
        onChange={e => setDraft(e.target.value)}
        rows={4}
        spellCheck={false}
        style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
        placeholder={'docs.python.org\nhttps://pypi.org'}
      />
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 6 }}>
        <button onClick={save} disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
        <span style={{ fontSize: 12, opacity: 0.75 }}>{msg}</span>
      </div>
    </div>
  );
}
