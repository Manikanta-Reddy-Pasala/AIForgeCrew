import { useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  api,
  AgentRole,
  AgentRoleConfig,
  integrationsApi,
  ProviderCatalog,
  ProviderId,
  ProviderModel,
} from '../api';
import { Icon } from '../icons';

// ── config-first Home page ─────────────────────────────────────────
//
// Lets any user pick provider + model per pipeline step — including a
// generic OpenAI-compatible endpoint — straight from the landing page,
// so they can clone + run without touching config files.
//
// Mirrors Settings.tsx styling and data-loading patterns.

const ROLE_ORDER: AgentRole[] = [
  'architect', 'planner', 'verifier', 'doer', 'feedback', 'learner',
];

const ROLE_HINTS: Record<AgentRole, string> = {
  architect: 'External operator session. Drives ticket creation; never edits code.',
  planner:   'Reads parent ticket; emits plan + child subtickets.',
  verifier:  'Plan critic. Single-turn judge BEFORE execution. Reject → re-plan.',
  doer:      'Edits code inside the subticket allowlist; runs compile + tests.',
  feedback:  'Post-execution judge. Verdict: pass | fail | scope_violation.',
  learner:   'Runs only on verdict=pass. Writes :Fact rows to memory.',
};

// Providers where base_url should be visible by default
const PROVIDERS_WITH_BASE_URL = new Set<ProviderId>(['openai_compatible', 'local']);
// Providers where api_key field is shown
const PROVIDERS_WITH_API_KEY = new Set<ProviderId>(['openai_compatible']);
// Providers where "Test connection" button appears
const PROVIDERS_WITH_TEST = new Set<ProviderId>(['openai_compatible']);

interface RowState {
  provider: ProviderId;
  model: string;
  base_url: string;
  api_key: string;
  api_key_set: boolean;
  insecure_tls: boolean;
  busy: boolean;
  justSavedAt: number | null;
  error: string | null;
  testBusy: boolean;
  testResult: { ok: boolean; models?: string[]; error?: string } | null;
}

function emptyRow(
  p: ProviderId,
  m: string,
  b: string | null,
  api_key_set = false,
  insecure_tls = false,
): RowState {
  return {
    provider: p,
    model: m,
    base_url: b ?? '',
    api_key: '',
    api_key_set,
    insecure_tls,
    busy: false,
    justSavedAt: null,
    error: null,
    testBusy: false,
    testResult: null,
  };
}

function isDirty(row: RowState, snap: AgentRoleConfig): boolean {
  return (
    row.provider !== snap.provider ||
    row.model !== snap.model ||
    (row.base_url || null) !== (snap.base_url || null) ||
    !!row.insecure_tls !== !!snap.insecure_tls
  );
}

function fmtCtx(n: number | null | undefined): string {
  if (!n) return '';
  if (n >= 1000) return `${Math.round(n / 1000)}K`;
  return String(n);
}

// ── "apply to all" top-bar state ─────────────────────────────────
interface BulkState {
  provider: ProviderId;
  model: string;
  base_url: string;
  api_key: string;
  insecure_tls: boolean;
  busy: boolean;
}

export default function Home() {
  const [providers, setProviders] = useState<ProviderCatalog[] | null>(null);
  const [snapshot, setSnapshot] =
    useState<Record<AgentRole, AgentRoleConfig> | null>(null);
  const [rows, setRows] = useState<Record<AgentRole, RowState>>(() =>
    Object.fromEntries(
      ROLE_ORDER.map(r => [r, emptyRow('local', '', null)]),
    ) as Record<AgentRole, RowState>,
  );
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);
  const savedTimers = useRef<Partial<Record<AgentRole, number>>>({});

  // Models discovered from the configured endpoint by the last successful
  // Test (bulk or per-row). One generic source: enter base_url + token,
  // Test, and every openai_compatible model dropdown fills from /v1/models —
  // no per-model hardcoding.
  const [discoveredModels, setDiscoveredModels] = useState<string[]>([]);

  // bulk "apply to all" widget
  const [bulk, setBulk] = useState<BulkState>({
    provider: 'local',
    model: '',
    base_url: '',
    api_key: '',
    insecure_tls: false,
    busy: false,
  });

  async function load(silent = false) {
    if (!silent) setLoading(true);
    setPageError(null);
    try {
      const [cfg, provs] = await Promise.all([
        api.agentsV2Config(),
        api.agentsV2Providers(),
      ]);
      setSnapshot(cfg);
      setProviders(provs);
      setRows(prev => {
        const next: Record<AgentRole, RowState> = { ...prev };
        for (const role of ROLE_ORDER) {
          const c = cfg[role];
          if (!c) continue;
          next[role] = emptyRow(
            c.provider, c.model, c.base_url, !!c.api_key_set, !!c.insecure_tls,
          );
        }
        return next;
      });
      // seed bulk widget with first provider's default
      if (provs.length > 0) {
        setBulk(b => ({
          ...b,
          provider: provs[0].id,
          model: provs[0].default_model || '',
        }));
      }
    } catch (e: any) {
      setPageError(e?.message || 'Failed to load config');
    } finally {
      if (!silent) setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);
  useEffect(() => () => {
    Object.values(savedTimers.current).forEach(t => t && clearTimeout(t));
  }, []);

  const provById = useMemo(() => {
    const m = new Map<ProviderId, ProviderCatalog>();
    (providers || []).forEach(p => m.set(p.id, p));
    return m;
  }, [providers]);

  function patch(role: AgentRole, next: Partial<RowState>) {
    setRows(r => ({ ...r, [role]: { ...r[role], ...next, error: null } }));
  }

  function changeProvider(role: AgentRole, providerId: ProviderId) {
    const cat = provById.get(providerId);
    const fallback = cat?.default_model || rows[role].model;
    patch(role, { provider: providerId, model: fallback, testResult: null });
  }

  async function saveOne(role: AgentRole): Promise<boolean> {
    if (!snapshot) return false;
    const row = rows[role];
    patch(role, { busy: true, error: null });
    try {
      const updated = await api.setAgentV2Config(role, {
        provider: row.provider,
        model: row.model,
        base_url: row.base_url.trim() || null,
        api_key: row.api_key.trim() || null,
        insecure_tls: row.insecure_tls,
      });
      setSnapshot(s => s ? ({
        ...s,
        [role]: {
          provider: updated.provider,
          model: updated.model,
          base_url: updated.base_url ?? null,
          api_key_set: updated.api_key_set,
          insecure_tls: updated.insecure_tls,
        },
      }) : s);
      patch(role, { busy: false, justSavedAt: Date.now(), api_key: '', api_key_set: !!updated.api_key_set, insecure_tls: !!updated.insecure_tls });
      const tid = window.setTimeout(() => {
        setRows(r => ({ ...r, [role]: { ...r[role], justSavedAt: null } }));
      }, 2000);
      const prev = savedTimers.current[role];
      if (prev) clearTimeout(prev);
      savedTimers.current[role] = tid;
      toast.success(`${role} saved`);
      return true;
    } catch (e: any) {
      const msg = e?.message || 'save failed';
      patch(role, { busy: false, error: msg });
      toast.error(`${role}: ${msg}`);
      return false;
    }
  }

  async function testConnection(role: AgentRole) {
    const row = rows[role];
    if (!row.base_url.trim()) {
      toast.warning('Enter a base_url before testing');
      return;
    }
    patch(role, { testBusy: true, testResult: null });
    try {
      const res = await api.providersTest(
        row.base_url.trim(),
        row.api_key.trim() || undefined,
        row.insecure_tls,
        role,  // server fills the blank token from this role's saved config
      );
      patch(role, { testBusy: false, testResult: res });
      if (res.ok) {
        const modelCount = res.models?.length ?? 0;
        if (res.models && res.models.length > 0) setDiscoveredModels(res.models);
        const msg = modelCount > 0
          ? `Reachable — ${modelCount} model${modelCount === 1 ? '' : 's'}`
          : 'Reachable';
        toast.success(msg);
        // offer to fill model with first result
        if (res.models && res.models.length > 0 && !row.model) {
          patch(role, { testBusy: false, testResult: res, model: res.models[0] });
        }
      } else {
        toast.error(`Connection failed: ${res.error || 'unknown error'}`);
      }
    } catch (e: any) {
      const errResult = { ok: false, error: e?.message || 'unknown' };
      patch(role, { testBusy: false, testResult: errResult });
      toast.error(`Test failed: ${e?.message || 'unknown'}`);
    }
  }

  // ── bulk apply ────────────────────────────────────────────────────
  function patchBulk(next: Partial<BulkState>) {
    setBulk(b => ({ ...b, ...next }));
  }

  function changeBulkProvider(providerId: ProviderId) {
    const cat = provById.get(providerId);
    patchBulk({ provider: providerId, model: cat?.default_model || '' });
  }

  async function testBulk() {
    if (!bulk.base_url.trim()) {
      toast.warning('Enter a base URL before testing');
      return;
    }
    setBulk(b => ({ ...b, busy: true }));
    try {
      const res = await api.providersTest(
        bulk.base_url.trim(),
        bulk.api_key.trim() || undefined,
        bulk.insecure_tls,
      );
      if (res.ok) {
        const n = res.models?.length ?? 0;
        if (res.models && res.models.length > 0) setDiscoveredModels(res.models);
        toast.success(n > 0 ? `Reachable — ${n} model${n === 1 ? '' : 's'}` : 'Reachable');
        if (res.models && res.models.length > 0 && !bulk.model) {
          patchBulk({ model: res.models[0] });
        }
      } else {
        toast.error(`Connection failed: ${res.error || 'unknown error'}`);
      }
    } catch (e: any) {
      toast.error(`Test failed: ${e?.message || 'unknown'}`);
    } finally {
      setBulk(b => ({ ...b, busy: false }));
    }
  }

  async function applyToAll() {
    setBulk(b => ({ ...b, busy: true }));
    let ok = 0, fail = 0;
    for (const role of ROLE_ORDER) {
      try {
        const updated = await api.setAgentV2Config(role, {
          provider: bulk.provider,
          model: bulk.model,
          base_url: bulk.base_url.trim() || null,
          api_key: bulk.api_key.trim() || null,
          insecure_tls: bulk.insecure_tls,
        });
        setSnapshot(s => s ? ({
          ...s,
          [role]: {
            provider: updated.provider,
            model: updated.model,
            base_url: updated.base_url ?? null,
            api_key_set: updated.api_key_set,
            insecure_tls: updated.insecure_tls,
          },
        }) : s);
        setRows(r => ({
          ...r,
          [role]: {
            ...r[role],
            provider: updated.provider,
            model: updated.model,
            base_url: updated.base_url ?? '',
            api_key: '',
            api_key_set: !!updated.api_key_set,
            insecure_tls: !!updated.insecure_tls,
            justSavedAt: Date.now(),
            error: null,
          },
        }));
        ok++;
      } catch {
        fail++;
      }
    }
    // Write the GLOBAL default (_default) + the chat slot to the same
    // endpoint. _default is inherited by every internal pipeline role
    // (triage / researcher / ctx_* / verify_* / gap_eval …) that isn't in
    // the visible table — so team-mode chat + tickets all use this one
    // endpoint instead of silently falling back to a dead `local`.
    for (const slot of ['_default', 'chat']) {
      try {
        await api.setAgentV2Config(slot as AgentRole, {
          provider: bulk.provider,
          model: bulk.model,
          base_url: bulk.base_url.trim() || null,
          api_key: bulk.api_key.trim() || null,
          insecure_tls: bulk.insecure_tls,
        });
      } catch { /* optional slots — don't fail the whole apply */ }
    }
    setBulk(b => ({ ...b, busy: false, api_key: '' }));
    if (ok && !fail) toast.success(`Applied to all ${ok} steps + chat`);
    else if (ok && fail) toast.warning(`Applied to ${ok}, ${fail} failed`);
    else toast.error('Apply failed for all steps');
  }

  // ── profile presets — bulk-assign one (provider, model) to all roles ─
  const [profiles, setProfiles] = useState<
    Array<{ name: string; provider: string; model: string }>
  >([]);
  const [profileBusy, setProfileBusy] = useState<string | null>(null);
  useEffect(() => {
    api.agentsV2Profiles()
      .then(d => setProfiles(d.profiles || []))
      .catch(() => { /* surface only on apply failure */ });
  }, []);
  async function applyProfile(name: string) {
    if (profileBusy) return;
    setProfileBusy(name);
    try {
      await api.applyAgentV2Profile(name);
      toast.success(`Profile "${name}" applied to all archetypes`);
      await load(true);
    } catch (e: any) {
      toast.error(`Profile apply failed: ${e?.message || 'unknown'}`);
    } finally {
      setProfileBusy(null);
    }
  }

  // ── runtime-wide backend toggle (fallback chain for every agent) ─────
  const [doerBackend, setDoerBackend] = useState<string>('local');
  const [backendOptions, setBackendOptions] = useState<string[]>(['local']);
  const [doerBackendBusy, setDoerBackendBusy] = useState(false);
  useEffect(() => {
    fetch('/api/runtime/llm_backend')
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d) return;
        setDoerBackend(d.backend || 'local');
        if (Array.isArray(d.options) && d.options.length > 0) {
          setBackendOptions(d.options);
        }
      })
      .catch(() => { /* endpoint might not exist on this build */ });
  }, []);
  const BACKEND_LABEL: Record<string, string> = {
    local:        'local (mlx-lm)',
    ollama_cloud: 'ollama cloud',
    openai:       'openai (API)',
    gemini:       'gemini (cloud Flash)',
  };
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
      toast.success(`Runtime backend → ${d.backend}`);
    } catch (e: any) {
      toast.error(`Switch failed: ${e.message}`);
    } finally {
      setDoerBackendBusy(false);
    }
  }

  const allProviders: ProviderCatalog[] = providers || [];
  const bulkCat = provById.get(bulk.provider);
  const bulkModels: ProviderModel[] = bulkCat?.models || [];

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Home</h1>
          <div className="subtitle">
            Configure the provider and model for each pipeline step. Changes
            persist to <code>~/.aiforge/agent_config.json</code> and take
            effect on the next agent run.
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="ghost" onClick={() => load()} disabled={loading}>
            <Icon.Refresh size={14} /> Reload
          </button>
        </div>
      </div>

      {pageError && (
        <div className="settings-banner">
          <Icon.Info size={14} />
          <span>{pageError}</span>
          <button className="ghost sm" onClick={() => load()}>
            Retry
          </button>
        </div>
      )}

      {/* ── Apply to all steps ─────────────────────────────────── */}
      <div className="card" style={{ marginBottom: 16 }}>
        <h2 style={{ fontSize: 14 }}>Apply to all steps</h2>
        <div className="subtitle" style={{ marginTop: 6, marginBottom: 12 }}>
          Pick a single provider + model and push it to every archetype at
          once. Individual rows below can still be overridden afterwards.
        </div>

        <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <label className="small muted">Provider</label>
            <select
              value={bulk.provider}
              onChange={e => changeBulkProvider(e.target.value as ProviderId)}
              disabled={bulk.busy || loading}
              style={{ minWidth: 200 }}
            >
              {allProviders.map(p => (
                <option key={p.id} value={p.id}>{p.label}</option>
              ))}
            </select>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <label className="small muted">Model</label>
            {(() => {
              // openai_compatible: prefer models discovered from the
              // configured endpoint (Test). Else the provider's catalog.
              const dyn = bulk.provider === 'openai_compatible'
                ? discoveredModels : [];
              const opts = dyn.length ? dyn : bulkModels.map(m => m.id);
              return opts.length > 0 ? (
                <select
                  value={bulk.model}
                  onChange={e => patchBulk({ model: e.target.value })}
                  disabled={bulk.busy}
                  style={{ minWidth: 220 }}
                >
                  {bulk.model && !opts.includes(bulk.model) && (
                    <option value={bulk.model}>{bulk.model} (custom)</option>
                  )}
                  {opts.map(id => (<option key={id} value={id}>{id}</option>))}
                </select>
              ) : (
                <input
                  value={bulk.model}
                  onChange={e => patchBulk({ model: e.target.value })}
                  disabled={bulk.busy}
                  placeholder={bulkCat?.default_model || 'Test to list models, or type id'}
                  style={{ minWidth: 220 }}
                />
              );
            })()}
          </div>

          {PROVIDERS_WITH_BASE_URL.has(bulk.provider) && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <label className="small muted">Base URL</label>
              <input
                value={bulk.base_url}
                onChange={e => patchBulk({ base_url: e.target.value })}
                disabled={bulk.busy}
                placeholder="http://localhost:1234/v1"
                style={{ minWidth: 240 }}
              />
            </div>
          )}

          {PROVIDERS_WITH_API_KEY.has(bulk.provider) && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <label className="small muted">API key / token</label>
              <input
                type="password"
                value={bulk.api_key}
                onChange={e => patchBulk({ api_key: e.target.value })}
                disabled={bulk.busy}
                placeholder="blank = no token"
                style={{ minWidth: 180 }}
              />
            </div>
          )}

          {PROVIDERS_WITH_BASE_URL.has(bulk.provider) && (
            <button
              className="ghost"
              onClick={testBulk}
              disabled={bulk.busy || !bulk.base_url.trim()}
              title="Test connection to this endpoint"
              style={{ alignSelf: 'flex-end', whiteSpace: 'nowrap' }}
            >
              {bulk.busy ? 'Testing…' : 'Test'}
            </button>
          )}

          <button
            onClick={applyToAll}
            disabled={bulk.busy || !bulk.model || loading}
            style={{ alignSelf: 'flex-end' }}
          >
            {bulk.busy ? 'Applying…' : 'Apply to all steps'}
          </button>
        </div>

        {PROVIDERS_WITH_BASE_URL.has(bulk.provider) && (
          <label
            className="small muted"
            style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 10 }}
            title="For a self-signed / internal HTTPS box (e.g. https://chatai.internal). Scoped to this endpoint only."
          >
            <input
              type="checkbox"
              checked={bulk.insecure_tls}
              onChange={e => patchBulk({ insecure_tls: e.target.checked })}
              disabled={bulk.busy}
            />
            Skip TLS verification (self-signed / internal HTTPS cert)
          </label>
        )}
      </div>

      {/* ── Profile presets (bundled provider+model combos) ────── */}
      {profiles.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <h2 style={{ fontSize: 14 }}>Profile preset</h2>
          <div className="subtitle" style={{ marginTop: 6, marginBottom: 10 }}>
            One-click assign a bundled provider + model to every archetype.
            Individual rows below can still be overridden afterwards.
          </div>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            {profiles.map(p => (
              <button
                key={p.name}
                className="ghost"
                onClick={() => applyProfile(p.name)}
                disabled={!!profileBusy}
                title={`${p.provider} → ${p.model}`}
              >
                {profileBusy === p.name ? `Applying ${p.name}…` : `Apply ${p.name}`}
                <span className="small muted" style={{ marginLeft: 6 }}>
                  ({p.provider})
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── Runtime-wide backend (fallback chain for every agent) ── */}
      <div className="card" style={{ marginBottom: 16 }}>
        <h2 style={{ fontSize: 14 }}>LLM backend (all agents)</h2>
        <div className="subtitle" style={{ marginTop: 6, marginBottom: 10 }}>
          Default backend used by every agent when its archetype row below
          has no explicit override. Options reflect what's actually
          installed/reachable on this host.
        </div>
        <div className="row" style={{ gap: 10, alignItems: 'center' }}>
          <label className="small muted">Backend</label>
          <select
            value={doerBackend}
            onChange={e => changeDoerBackend(e.target.value)}
            disabled={doerBackendBusy}
            style={{ minWidth: 240 }}
          >
            {backendOptions.map(opt => (
              <option key={opt} value={opt}>{BACKEND_LABEL[opt] || opt}</option>
            ))}
          </select>
          {doerBackendBusy && <span className="small muted">switching…</span>}
        </div>
      </div>

      {/* ── Per-archetype config table ─────────────────────────── */}
      <div className="settings-table">
        <div className="settings-table-head">
          <div>Archetype</div>
          <div>Provider</div>
          <div>Model</div>
          <div className="col-meta">Tier · Context</div>
          <div>Endpoint / Key</div>
          <div style={{ textAlign: 'right' }}>Save</div>
        </div>

        {loading || !snapshot
          ? Array.from({ length: ROLE_ORDER.length }).map((_, i) => (
              <div className="settings-row" key={`sk-${i}`}>
                <div className="role-cell">
                  <div className="skeleton" style={{ height: 14, width: '60%' }} />
                  <div className="skeleton" style={{ height: 10, width: '90%', marginTop: 6 }} />
                </div>
                <div className="skeleton" style={{ height: 30 }} />
                <div className="skeleton" style={{ height: 30 }} />
                <div className="skeleton" style={{ height: 18, width: 80 }} />
                <div className="skeleton" style={{ height: 30 }} />
                <div className="skeleton" style={{ height: 30, width: 60 }} />
              </div>
            ))
          : ROLE_ORDER.map(role => {
              const row = rows[role];
              const snap = snapshot[role];
              const dirty = isDirty(row, snap);
              const cat = provById.get(row.provider);
              const models: ProviderModel[] = cat?.models || [];
              const current = models.find(m => m.id === row.model);
              const showBaseUrl = PROVIDERS_WITH_BASE_URL.has(row.provider);
              const showApiKey = PROVIDERS_WITH_API_KEY.has(row.provider);
              const showTest = PROVIDERS_WITH_TEST.has(row.provider);

              const cls = [
                'settings-row',
                `prov-${row.provider}`,
                dirty ? 'dirty' : '',
                row.justSavedAt ? 'just-saved' : '',
              ].filter(Boolean).join(' ');

              return (
                <div key={role} className={cls}>
                  {/* archetype name + hint */}
                  <div className="role-cell">
                    <span className="role-name" title={ROLE_HINTS[role]}>
                      {role}
                      {dirty && <span className="chip sm warn">unsaved</span>}
                    </span>
                    <span className="role-hint">{ROLE_HINTS[role]}</span>
                  </div>

                  {/* provider select */}
                  <select
                    value={row.provider}
                    onChange={e => changeProvider(role, e.target.value as ProviderId)}
                    disabled={row.busy}
                    aria-label={`${role} provider`}
                  >
                    {allProviders.map(p => (
                      <option key={p.id} value={p.id}>{p.label}</option>
                    ))}
                  </select>

                  {/* model — dropdown from discovered (openai_compatible)
                      or provider catalog; free-text until discovered */}
                  {(() => {
                    const dyn = row.provider === 'openai_compatible'
                      ? discoveredModels : [];
                    const opts = dyn.length ? dyn : models.map(m => m.id);
                    return opts.length > 0 ? (
                      <select
                        value={row.model}
                        onChange={e => patch(role, { model: e.target.value })}
                        disabled={row.busy}
                        aria-label={`${role} model`}
                      >
                        {row.model && !opts.includes(row.model) && (
                          <option value={row.model}>{row.model} (custom)</option>
                        )}
                        {opts.map(id => (<option key={id} value={id}>{id}</option>))}
                      </select>
                    ) : (
                      <input
                        value={row.model}
                        onChange={e => patch(role, { model: e.target.value })}
                        disabled={row.busy}
                        placeholder={cat?.default_model || 'Test to list models'}
                        aria-label={`${role} model`}
                      />
                    );
                  })()}

                  {/* tier + context chips */}
                  <div className="meta-chips">
                    {current?.tier && (
                      <span className={`chip sm tier-${current.tier}`}>
                        {current.tier}
                      </span>
                    )}
                    {current?.context && (
                      <span className="chip sm mono">{fmtCtx(current.context)}</span>
                    )}
                  </div>

                  {/* endpoint + api_key column */}
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {showBaseUrl && (
                      <div className="row" style={{ gap: 6, alignItems: 'center' }}>
                        <input
                          value={row.base_url}
                          onChange={e => patch(role, { base_url: e.target.value, testResult: null })}
                          disabled={row.busy}
                          placeholder={
                            row.provider === 'local'
                              ? 'http://127.0.0.1:1234/v1 (default)'
                              : 'https://your-api.example.com/v1'
                          }
                          aria-label={`${role} base url`}
                          style={{ flex: 1, minWidth: 0 }}
                        />
                        {showTest && (
                          <button
                            className="ghost sm"
                            onClick={() => testConnection(role)}
                            disabled={row.testBusy || row.busy}
                            title="Test connection"
                            style={{ whiteSpace: 'nowrap' }}
                          >
                            {row.testBusy ? 'Testing…' : 'Test'}
                          </button>
                        )}
                      </div>
                    )}
                    {showApiKey && (
                      <input
                        type="password"
                        value={row.api_key}
                        onChange={e => patch(role, { api_key: e.target.value })}
                        disabled={row.busy}
                        placeholder={
                          row.api_key_set
                            ? '•••••• (saved — enter new to replace)'
                            : 'blank = no token'
                        }
                        aria-label={`${role} api key`}
                      />
                    )}
                    {showBaseUrl && (
                      <label
                        className="small muted"
                        style={{ display: 'flex', alignItems: 'center', gap: 6 }}
                        title="Skip TLS verification for this endpoint (self-signed / internal HTTPS box). Scoped to this host only."
                      >
                        <input
                          type="checkbox"
                          checked={row.insecure_tls}
                          onChange={e => patch(role, { insecure_tls: e.target.checked, testResult: null })}
                          disabled={row.busy}
                          aria-label={`${role} skip TLS verify`}
                        />
                        Skip TLS verify (self-signed)
                      </label>
                    )}
                    {/* show test result inline */}
                    {row.testResult && (
                      <div
                        className="small"
                        style={{
                          color: row.testResult.ok
                            ? 'var(--green, #4ade80)'
                            : 'var(--red, #f87171)',
                        }}
                      >
                        {row.testResult.ok
                          ? row.testResult.models?.length
                            ? `Reachable — ${row.testResult.models.length} model${row.testResult.models.length === 1 ? '' : 's'}: ${row.testResult.models.slice(0, 3).join(', ')}${row.testResult.models.length > 3 ? '…' : ''}`
                            : 'Reachable'
                          : `Error: ${row.testResult.error || 'unknown'}`}
                        {row.testResult.ok && row.testResult.models && row.testResult.models.length > 0 && !row.model && (
                          <button
                            className="ghost sm"
                            onClick={() =>
                              patch(role, {
                                model: row.testResult!.models![0],
                                testResult: row.testResult,
                              })
                            }
                            style={{ marginLeft: 6 }}
                          >
                            Use {row.testResult.models[0]}
                          </button>
                        )}
                      </div>
                    )}
                    {/* show a muted dash when no endpoint controls for this provider */}
                    {!showBaseUrl && !showApiKey && (
                      <span className="small muted">—</span>
                    )}
                  </div>

                  {/* save cell */}
                  <div className="save-cell">
                    {row.justSavedAt && (
                      <span className="saved-pill" key={row.justSavedAt}>
                        <Icon.Check size={11} /> Saved
                      </span>
                    )}
                    <button
                      className="sm"
                      onClick={() => saveOne(role)}
                      disabled={!dirty || row.busy}
                    >
                      {row.busy ? 'Saving…' : 'Save'}
                    </button>
                  </div>

                  {row.error && (
                    <div className="row-error">{row.error}</div>
                  )}
                </div>
              );
            })}
      </div>

      {/* Integrations */}
      <div className="card" style={{ marginTop: 16 }}>
        <h2 style={{ fontSize: 14 }}>Integrations</h2>
        <ConfluenceCard />
      </div>

      {/* provider reference */}
      <div className="card" style={{ marginTop: 16 }}>
        <h2 style={{ fontSize: 14 }}>Providers</h2>
        <ul className="small muted" style={{ marginTop: 6, paddingLeft: 18 }}>
          <li><code>local</code> — mlx-lm on Mac Studio. Catalog auto-discovered from <code>http://127.0.0.1:1234/v1/models</code>.</li>
          <li><code>ollama_cloud</code> — Ollama Cloud. Requires <code>OLLAMA_CLOUD_API_KEY</code>; catalog cached for 5 min.</li>
          <li><code>openai_compatible</code> — Any OpenAI-compatible endpoint (OpenRouter, Groq, Together, vLLM, cloud-with-key). Enter a base URL and optional API key; use <em>Test</em> to verify.</li>
        </ul>
      </div>
    </>
  );
}

// ── Confluence integration card ──────────────────────────────────────────────
function ConfluenceCard() {
  const [baseUrl, setBaseUrl] = useState('');
  const [token, setToken] = useState('');
  const [user, setUser] = useState('');
  const [authMode, setAuthMode] = useState<'pat' | 'basic'>('pat');
  const [insecure, setInsecure] = useState(false);
  const [hasToken, setHasToken] = useState(false);
  const [envManaged, setEnvManaged] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    integrationsApi.getConfluence().then(c => {
      setBaseUrl(c.base_url || '');
      setUser(c.user || '');
      setAuthMode(c.user ? 'basic' : 'pat');   // a stored user ⇒ basic was used
      setInsecure(!!c.insecure_tls);
      setHasToken(!!c.has_token);
      setEnvManaged(!!c.env_managed);
    }).catch(() => { /* endpoint may be absent on old API */ });
  }, []);

  async function save() {
    setBusy(true);
    try {
      // PAT/Bearer ⇒ ALWAYS clear the user (a non-empty user forces Basic
      // auth on the server, which a PAT can't satisfy → 401).
      const c = await integrationsApi.setConfluence({
        base_url: baseUrl.trim(),
        user: authMode === 'basic' ? user.trim() : '',
        insecure_tls: insecure,
        ...(token.trim() ? { token: token.trim() } : {}),
      });
      setHasToken(!!c.has_token);
      setToken('');
      toast.success('Confluence settings saved');
    } catch (e: any) {
      toast.error(`Save failed: ${e.message}`);
    } finally { setBusy(false); }
  }

  async function test() {
    setBusy(true);
    try {
      const r = await integrationsApi.testConfluence();
      if (r.ok) toast.success(`Connected to ${r.base_url || 'Confluence'} (${r.auth} auth)`);
      else {
        const extra = r.denied_reason ? ` [${r.denied_reason}]` : (r.detail ? ` — ${r.detail}` : '');
        toast.error(`${r.error || 'Test failed'} (${r.auth} auth)${extra}${r.hint ? ` — ${r.hint}` : ''}`, { duration: 14000 });
      }
    } catch (e: any) {
      toast.error(`Test failed: ${e.message}`);
    } finally { setBusy(false); }
  }

  const inputStyle = { width: '100%', maxWidth: 460, padding: '6px 8px', fontSize: 13 };

  return (
    <div style={{ marginTop: 6 }}>
      <div className="small muted" style={{ marginBottom: 8 }}>
        <strong>Confluence</strong> (Server / Data Center) — chat tools to search, read,
        create &amp; update pages. Writes go through the chat approval gate.
        {envManaged && <span style={{ color: 'var(--warn, #f59e0b)' }}> · currently set via env (overrides this form)</span>}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 460 }}>
        <label className="small">Base URL
          <input style={inputStyle} placeholder="https://confluence.yourco.internal"
                 value={baseUrl} onChange={e => setBaseUrl(e.target.value)} />
        </label>
        <label className="small">Auth
          <select style={inputStyle} value={authMode}
                  onChange={e => setAuthMode(e.target.value as 'pat' | 'basic')}>
            <option value="pat">Personal Access Token (Bearer) — recommended</option>
            <option value="basic">Username + Password (Basic)</option>
          </select>
        </label>
        <label className="small">{authMode === 'pat' ? 'Personal Access Token' : 'Password'}
          <input style={inputStyle} type="password"
                 placeholder={hasToken ? '•••••• (leave blank to keep)' : (authMode === 'pat' ? 'paste PAT' : 'password')}
                 value={token} onChange={e => setToken(e.target.value)} />
        </label>
        {authMode === 'basic' && (
          <label className="small">Username
            <input style={inputStyle} placeholder="you@company.com"
                   value={user} onChange={e => setUser(e.target.value)} />
          </label>
        )}
        <label className="row small" style={{ gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={insecure} onChange={e => setInsecure(e.target.checked)} />
          Skip TLS verify (self-signed internal cert)
        </label>
        <div className="row" style={{ gap: 8 }}>
          <button onClick={save} disabled={busy || !baseUrl.trim()}>Save</button>
          <button className="ghost" onClick={test} disabled={busy}>Test connection</button>
        </div>
        <div className="xs muted">
          Create a token in Confluence: avatar → Settings → Personal Access Tokens → Create token.
        </div>
      </div>
    </div>
  );
}
