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

  // 9 archetype roles — order matches the pipeline flow.
  const roleOrder = [
    'understander', 'planner', 'verifier', 'grounder',
    'doer', 'validator', 'tester', 'architect', 'learner',
  ];
  const provKeys = Object.keys(providers);

  // Doer backend toggle: local mlx-lm ↔ cloud Gemini-Flash. Hits a
  // dedicated runtime endpoint (separate from per-role provider).
  const [doerBackend, setDoerBackend] = useState<string>('local');
  const [geminiAvailable, setGeminiAvailable] = useState<boolean>(false);
  const [doerBackendBusy, setDoerBackendBusy] = useState(false);
  useEffect(() => {
    fetch('/api/runtime/llm_backend')
      .then(r => r.json())
      .then(d => {
        setDoerBackend(d.backend || 'local');
        setGeminiAvailable(!!d.gemini_available);
      })
      .catch(() => {});
  }, []);
  async function changeDoerBackend(next: string) {
    setDoerBackendBusy(true);
    try {
      const r = await fetch('/api/runtime/llm_backend', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend: next }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setDoerBackend(d.backend);
      toast.success(`Doer backend → ${d.backend}`);
    } catch (e: any) {
      toast.error(`Switch failed: ${e.message}`);
    } finally {
      setDoerBackendBusy(false);
    }
  }

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

      <div className="card" style={{ marginTop: 16 }}>
        <h2 style={{ fontSize: 14 }}>LLM backend (all agents)</h2>
        <div className="subtitle" style={{ marginTop: 6, marginBottom: 10 }}>
          Flips planner / doer / feedback / learner / chat at once.
          <code> local</code> = mlx-lm on Mac Studio with Gemini-Flash
          as fallback when the local model errors.
          <code> gemini</code> = Gemini-Flash primary, mlx-lm as
          fallback. Use to A/B compare cloud vs local. Changes apply
          to runs started after the switch.
        </div>
        <div className="row" style={{ gap: 10, alignItems: 'center' }}>
          <label className="small muted">Backend</label>
          <select
            value={doerBackend}
            onChange={e => changeDoerBackend(e.target.value)}
            disabled={doerBackendBusy}
            style={{ minWidth: 180 }}
          >
            <option value="local">local (mlx-lm)</option>
            <option value="gemini" disabled={!geminiAvailable}>
              gemini (cloud Flash){!geminiAvailable && ' — no API key'}
            </option>
          </select>
          {doerBackendBusy && <span className="small muted">switching…</span>}
        </div>
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
