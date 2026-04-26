import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { api } from '../api';
import { Icon } from '../icons';

// Per-agent model + provider config editor. Backed by
// /api/config/agents — GET fetches full map, PUT updates one role.
// Env vars still override at read time on the server, so ops has a
// final-say escape hatch.
export default function Settings() {
  const [roles, setRoles] = useState<Record<string, { provider: string; model: string }>>({});
  const [providers, setProviders] = useState<Record<string, { label: string; default_model: string }>>({});
  const [dirty, setDirty] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  async function load() {
    try {
      const r = await api.agentConfig();
      setRoles(r.roles || {});
      setProviders(r.providers || {});
      setDirty({});
    } catch (e: any) {
      toast.error(`Config load failed: ${e.message}`);
    }
  }
  useEffect(() => { load(); }, []);

  function patch(role: string, next: Partial<{ provider: string; model: string }>) {
    setRoles(r => ({ ...r, [role]: { ...r[role], ...next } }));
    setDirty(d => ({ ...d, [role]: true }));
  }

  async function save(role: string) {
    setBusy(b => ({ ...b, [role]: true }));
    try {
      const row = roles[role];
      await api.setAgentConfig(role, row.provider, row.model);
      setDirty(d => ({ ...d, [role]: false }));
      toast.success(`Saved ${role}: ${row.provider}/${row.model}`);
    } catch (e: any) {
      toast.error(`${role} save failed: ${e.message}`);
    } finally {
      setBusy(b => ({ ...b, [role]: false }));
    }
  }

  const roleOrder = ['supervisor', 'planner', 'doer', 'feedback', 'learner', 'chat'];
  const provKeys = Object.keys(providers);

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <div className="subtitle">
            Pick provider + model per agent role. Changes persist to{' '}
            <code>~/.aiforge/agent_config.json</code> and are picked up on the
            next agent invocation.
          </div>
        </div>
        <button className="ghost" onClick={load}>
          <Icon.Refresh size={14} /> Reload
        </button>
      </div>

      <div className="stack" style={{ gap: 12 }}>
        {roleOrder.filter(r => roles[r]).map(role => {
          const row = roles[role];
          const provInfo = providers[row.provider];
          return (
            <div key={role} className="card">
              <div className="card-header">
                <div>
                  <div className="row" style={{ gap: 10 }}>
                    <h2 className="mono" style={{ fontSize: 16 }}>{role}</h2>
                    {dirty[role] && <span className="chip sm warn">unsaved</span>}
                  </div>
                  <div className="subtitle" style={{ marginTop: 4 }}>
                    Current: <code>{row.provider}</code> · <code>{row.model}</code>
                  </div>
                </div>
                <button
                  onClick={() => save(role)}
                  disabled={!dirty[role] || busy[role]}
                >
                  {busy[role] ? 'Saving…' : 'Save'}
                </button>
              </div>

              <div className="field-row">
                <label className="field">
                  <span>Provider</span>
                  <select
                    value={row.provider}
                    onChange={e => {
                      const p = e.target.value;
                      const def = providers[p]?.default_model || row.model;
                      patch(role, { provider: p, model: def });
                    }}
                  >
                    {provKeys.map(p => (
                      <option key={p} value={p}>
                        {providers[p].label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Model</span>
                  <input
                    value={row.model}
                    onChange={e => patch(role, { model: e.target.value })}
                    placeholder={provInfo?.default_model || 'model id'}
                  />
                </label>
              </div>
            </div>
          );
        })}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 style={{ fontSize: 14 }}>Providers</h2>
        <ul className="small muted" style={{ marginTop: 6, paddingLeft: 18 }}>
          <li><code>local</code> — mlx-lm on Mac Studio (reachable via <code>http://127.0.0.1:1234/v1</code> through SSH tunnel on NUC).</li>
          <li><code>anthropic</code> — Claude via LiteLLM. Requires <code>ANTHROPIC_API_KEY</code> in the server env.</li>
          <li><code>ollama_cloud</code> — Ollama Cloud. Requires <code>OLLAMA_CLOUD_API_KEY</code>.</li>
        </ul>
      </div>
    </>
  );
}
