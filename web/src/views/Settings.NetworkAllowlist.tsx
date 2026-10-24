/**
 * The NETWORK allowlist: what the box can reach at all, enforced by the egress
 * proxy in isolated docker mode (`./run.sh --isolated`). Every command the
 * agent runs — curl, wget, pip, git, a script it wrote — goes through it.
 *
 * The list is kept by the egress controller OUTSIDE the box and reached from
 * this browser through the UI front door, which refuses the box's own network:
 * you can change it here, the agent cannot change it at all.
 *
 * Not isolated (the default docker mode shares this machine's network): the
 * section says so plainly — then only AIForge's own web tools are limited, and
 * a command the agent runs can reach any site.
 */
import { useEffect, useState } from 'react';
import {
  networkAllowlist, setNetworkAllowlist, networkAllowChanges,
  NetworkAllowlist as NetList, NetworkAllowChange,
} from '../api/client';

export function NetworkAllowlist({ suggest, onSaved }: {
  /** hosts AIForge itself needs (integrations, …): offered as one-click adds */
  suggest: string[];
  /** keep AIForge's own web-tool list in step with the network list */
  onSaved?: (hosts: string[]) => void;
}) {
  const [net, setNet] = useState<NetList | null | undefined>(undefined);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [changes, setChanges] = useState<NetworkAllowChange[]>([]);

  async function load() {
    const n = await networkAllowlist();
    setNet(n);
    if (n) {
      setDraft(n.hosts.join('\n'));
      setChanges(await networkAllowChanges());
    }
  }
  useEffect(() => { load(); }, []);

  async function save(hosts: string[]) {
    setBusy(true);
    setMsg('');
    try {
      const n = await setNetworkAllowlist(hosts);
      setNet(n);
      setDraft(n.hosts.join('\n'));
      setMsg(`saved — the network now allows ${n.hosts.length} host(s); applied at once`);
      setChanges(await networkAllowChanges());
      onSaved?.(n.hosts);
    } catch (e: any) {
      setMsg(`save failed: ${e?.message || e}`);
    } finally {
      setBusy(false);
    }
  }

  if (net === undefined) return null;
  if (net === null) {
    return (
      <div role="note" style={{ border: '1px solid var(--warn, #d4a72c)', borderRadius: 6, padding: '8px 10px',
                                 margin: '6px 0 12px', fontSize: 13 }}>
        <strong>Not enforced at the network.</strong> This box shares this machine's network
        (default docker mode), so the list below limits only AIForge's own web tools — a
        command the agent runs (curl, pip, a script it wrote) can still reach any site.
        To enforce an allowlist for everything, start AIForge with <code>./run.sh --isolated</code>;
        this card then edits the network allowlist itself.
      </div>
    );
  }
  const typed = draft.split('\n').map(s => s.trim()).filter(Boolean);
  const missing = suggest.filter(h => !net.hosts.includes(h));
  return (
    <div style={{ margin: '6px 0 14px' }}>
      <h4 style={{ margin: '4px 0' }}>Network allowlist <span className="chip ok" style={{ fontSize: 11 }}>enforced</span></h4>
      <p style={{ fontSize: 13, opacity: 0.85, margin: '2px 0 6px' }}>
        The box can reach ONLY these hosts (and their subdomains) — for every command the
        agent runs: curl, wget, pip, git, scripts. Everything else is refused by the network.
        Localhost and the model need no entry. The agent cannot change this list; only this page can.
      </p>
      <textarea
        aria-label="Network allowlist, one host per line"
        value={draft}
        onChange={e => setDraft(e.target.value)}
        rows={Math.min(10, Math.max(4, typed.length + 1))}
        spellCheck={false}
        style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
        placeholder={'pypi.org\nfiles.pythonhosted.org\nartifactory.example.com'}
      />
      {missing.length > 0 && (
        <div style={{ fontSize: 12, margin: '4px 0', display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
          <span style={{ opacity: 0.7 }}>Your integrations use these — not reachable yet:</span>
          {missing.map(h => (
            <button key={h} type="button" className="ghost sm" disabled={busy}
                    onClick={() => save([...net.hosts, h])} title={`Allow ${h} on the network`}>+ {h}</button>
          ))}
        </div>
      )}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 6 }}>
        <button type="button" onClick={() => save(typed)} disabled={busy}>{busy ? 'Saving…' : 'Save network allowlist'}</button>
        <span style={{ fontSize: 12, opacity: 0.8 }}>{msg}</span>
      </div>
      {changes.length > 0 && (
        <details style={{ marginTop: 6, fontSize: 12 }}>
          <summary style={{ cursor: 'pointer', opacity: 0.75 }}>Recent changes</summary>
          {changes.slice(0, 15).map((c, i) => (
            <div key={i} style={{ opacity: 0.85 }}>
              {c.at} · {c.by || '?'}
              {c.added.length > 0 && <span style={{ color: 'var(--ok)' }}> +{c.added.join(', +')}</span>}
              {c.removed.length > 0 && <span style={{ color: 'var(--err)' }}> −{c.removed.join(', −')}</span>}
            </div>
          ))}
        </details>
      )}
    </div>
  );
}
