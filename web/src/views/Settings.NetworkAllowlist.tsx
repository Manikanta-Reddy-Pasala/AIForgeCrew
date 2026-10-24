/**
 * The NETWORK allowlist: what the box can reach at all, enforced by the egress
 * proxy in isolated docker mode (`./run.sh --isolated`). Every command the
 * agent runs — curl, wget, pip, git, a script it wrote — goes through it.
 *
 * Edited here; saved to the egress controller OUTSIDE the box through the UI
 * front door, which refuses the box's own network and the proxy, so the agent
 * cannot change it. Not isolated: says so plainly — then only AIForge's own
 * web tools are limited, and a command the agent runs can reach any site.
 */
import { useEffect, useState } from 'react';
import { setNetworkAllowlist, networkAllowChanges } from '../api/client';
import type { EgressHosts, NetworkAllowChange } from '../api/client';

export function NetworkAllowlist({ network, suggest, onSaved }: {
  network: EgressHosts['network'];
  /** hosts AIForge itself uses (integrations): offered as one-click adds */
  suggest: string[];
  onSaved?: () => void;
}) {
  const [hosts, setHosts] = useState<string[]>(network?.hosts || []);
  const [draft, setDraft] = useState((network?.hosts || []).join('\n'));
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [changes, setChanges] = useState<NetworkAllowChange[]>([]);

  // Keyed on the list's CONTENT: a refetch of the same list must not throw
  // away what is being typed.
  const serverKey = (network?.hosts || []).join('\n');
  useEffect(() => {
    if (!network) return;
    setHosts(network.hosts);
    setDraft(serverKey);
    networkAllowChanges().then(setChanges);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey, !!network]);

  async function save(next: string[]) {
    setBusy(true);
    setMsg('');
    try {
      const saved = await setNetworkAllowlist(next);
      setHosts(saved);
      setDraft(saved.join('\n'));
      setMsg(`saved — the network allows ${saved.length} host(s); applied within 2 seconds`);
      setChanges(await networkAllowChanges());
      onSaved?.();
    } catch (e: any) {
      setMsg(`save failed: ${e?.message || e}`);
    } finally {
      setBusy(false);
    }
  }

  if (!network) {
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
  const missing = suggest.filter(h => !hosts.includes(h));
  return (
    <div style={{ margin: '6px 0 14px' }}>
      <h4 style={{ margin: '4px 0' }}>Network allowlist <span className="chip ok" style={{ fontSize: 11 }}>enforced</span></h4>
      <p style={{ fontSize: 13, opacity: 0.85, margin: '2px 0 6px' }}>
        The ONE list for everything the box reaches: web, git (https or ssh), ssh, scp, rsync,
        pip, npm, scripts — only these hosts (and their subdomains). Everything else is refused
        by the network. Localhost and the model need no entry. The agent cannot change this list.
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
                    onClick={() => save([...typed.filter(x => x !== h), h])}
                    title={`Allow ${h} on the network (keeps what you typed)`}>+ {h}</button>
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
