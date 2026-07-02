import { useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  api,
  chatApi,
  AgentRole,
  AgentRoleConfig,
  ProviderCatalog,
  ProviderId,
  ProviderModel,
} from '../api';
import { Icon } from '../icons';
import { JiraCard, ConfluenceCard, GitlabCard, EmailCard } from '../components/Integrations';
import AgentSettings from './AgentSettings';

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

// openai_compatible is the only provider — base URL, API key and the
// Test button all apply to it.
const PROVIDERS_WITH_BASE_URL = new Set<ProviderId>(['openai_compatible']);
const PROVIDERS_WITH_API_KEY = new Set<ProviderId>(['openai_compatible']);
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
  const [tab, setTab] = useState<'agent' | 'integrations'>('agent');
  const [providers, setProviders] = useState<ProviderCatalog[] | null>(null);
  const [snapshot, setSnapshot] =
    useState<Record<AgentRole, AgentRoleConfig> | null>(null);
  const [rows, setRows] = useState<Record<AgentRole, RowState>>(() =>
    Object.fromEntries(
      ROLE_ORDER.map(r => [r, emptyRow('openai_compatible', '', null)]),
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
    provider: 'openai_compatible',
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

  // ── reset all saved per-role config (clean reconfigure) ─────────────
  const [resetBusy, setResetBusy] = useState(false);
  async function resetConfig() {
    if (resetBusy) return;
    if (!window.confirm(
      'Reset all agent config? This deletes every saved per-role setting so '
      + 'stale rows can\'t shadow the model you set next. You\'ll reconfigure '
      + 'from a clean slate.')) return;
    setResetBusy(true);
    try {
      const r = await api.resetAgentsV2(false);
      toast.success(r.removed ? 'Agent config reset — configure your model below.'
                              : (r.note || 'Nothing to reset.'));
      await load(true);
    } catch (e: any) {
      toast.error(`Reset failed: ${e?.message || 'unknown'}`);
    } finally {
      setResetBusy(false);
    }
  }

  const allProviders: ProviderCatalog[] = providers || [];
  const bulkCat = provById.get(bulk.provider);
  const bulkModels: ProviderModel[] = bulkCat?.models || [];

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <div className="subtitle">
            {tab === 'agent'
              ? <>Configure the provider and model for each pipeline step. Changes persist to <code>~/.aiforge/agent_config.json</code> and take effect on the next agent run.</>
              : <>Connect Jira, Confluence and GitLab so the chat agent can search, read and update them.</>}
          </div>
        </div>
        {tab === 'agent' && (
          <div className="row" style={{ gap: 8 }}>
            <button className="ghost" onClick={() => load()} disabled={loading}>
              <Icon.Refresh size={14} /> Reload
            </button>
            <button
              className="ghost"
              onClick={resetConfig}
              disabled={resetBusy}
              title="Delete all saved per-role config so stale rows can't shadow the model you set next"
              style={{ color: 'var(--err, #ef4444)' }}
            >
              {resetBusy ? 'Resetting…' : '↺ Reset all config'}
            </button>
          </div>
        )}
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

      {/* ── Settings tabs: Agent | Integrations ──────────────────── */}
      <div className="row" style={{ gap: 4, marginBottom: 16, borderBottom: '1px solid var(--border-1)' }}>
        {(['agent', 'integrations'] as const).map(t => (
          <button key={t} onClick={() => setTab(t)}
                  className={tab === t ? '' : 'ghost'}
                  style={{ borderRadius: '6px 6px 0 0', textTransform: 'capitalize',
                           fontWeight: tab === t ? 600 : 400 }}>
            {t}
          </button>
        ))}
      </div>

      {tab === 'integrations' && <IntegrationsTab />}

      {tab === 'agent' && (<>
        {/* Simplified: add models once, each agent picks one by name. */}
        <AgentSettings />
        {/* ── Global LLM token knobs (output cap + input window) ── */}
        <LlmSettingsCard />
      </>)}

      {/* Legacy provider/archetype editor — superseded by AgentSettings. */}
      {false && (<>

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

      {/* ── Orchestrator model (enhancer + architect + planner) ── */}
      <OrchestratorModelCard />

      {/* ── Global LLM token knobs (output cap + input window) ── */}
      <LlmSettingsCard />


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
                          placeholder="http://127.0.0.1:1234/v1 or https://your-api.example.com/v1"
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

      {/* provider reference */}
      <div className="card" style={{ marginTop: 16 }}>
        <h2 style={{ fontSize: 14 }}>Providers</h2>
        <ul className="small muted" style={{ marginTop: 6, paddingLeft: 18 }}>
          <li><code>openai_compatible</code> — Any OpenAI-compatible endpoint (local LM Studio / mlx-lm, OpenRouter, Groq, Together, vLLM, cloud-with-key). Enter a base URL and optional API key; use <em>Test</em> to verify.</li>
        </ul>
      </div>

      </>)}
    </>
  );
}

// Integrations tab — pick Jira / Confluence / GitLab / Email, configure one at a time.
function IntegrationsTab() {
  const [sub, setSub] = useState<'jira' | 'confluence' | 'gitlab' | 'email'>('jira');
  const label = (s: string) =>
    s === 'gitlab' ? 'GitLab' : s === 'email' ? 'Email' : s;
  return (
    <div className="card">
      <div className="row" style={{ gap: 4, marginBottom: 12 }}>
        {(['jira', 'confluence', 'gitlab', 'email'] as const).map(s => (
          <button key={s} onClick={() => setSub(s)}
                  className={sub === s ? '' : 'ghost'}
                  style={{ textTransform: 'capitalize', fontWeight: sub === s ? 600 : 400 }}>
            {label(s)}
          </button>
        ))}
      </div>
      {sub === 'jira' && <JiraCard />}
      {sub === 'confluence' && <ConfluenceCard />}
      {sub === 'gitlab' && <GitlabCard />}
      {sub === 'email' && <EmailCard />}
    </div>
  );
}

// ── Orchestrator model card ──────────────────────────────────────────────────
// The orchestrator = the layer-1 agents (enhancer + architect + planner) that
// analyze/enhance a request and split it into subtasks. Picking a model here
// sets all of them at once via PUT /api/chat/orchestrator-model — letting the
// splitter run on a stronger reasoning model than the workers.
function OrchestratorModelCard() {
  const [models, setModels] = useState<Array<{ id: string; label: string }>>([]);
  const [current, setCurrent] = useState<string>('');
  const [roles, setRoles] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  async function load() {
    try {
      const d = await chatApi.orchestratorModel();
      setModels(d.models || []);
      setCurrent(d.model || '');
      setRoles(d.roles || []);
    } catch {
      /* endpoint absent on this build — card stays empty */
    } finally {
      setLoaded(true);
    }
  }
  useEffect(() => { load(); }, []);

  async function change(next: string) {
    if (!next || next === current) return;
    setBusy(true);
    try {
      await chatApi.setOrchestratorModel(next);
      setCurrent(next);
      toast.success(`Orchestrator model → ${next}`);
    } catch (e: any) {
      toast.error(`Switch failed: ${e?.message || 'unknown'}`);
    } finally {
      setBusy(false);
    }
  }

  // Make sure the current selection is always present as an option.
  const opts = current && !models.some(m => m.id === current)
    ? [{ id: current, label: current.split('/').pop() || current }, ...models]
    : models;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2 style={{ fontSize: 14 }}>Orchestrator model</h2>
      <div className="subtitle" style={{ marginTop: 6, marginBottom: 10 }}>
        Model used by the orchestrator{roles.length ? ` (${roles.join(' + ')})` : ''} —
        the agents that enhance the request and split it into subtasks. Picks
        from the same model universe as chat.
      </div>
      <div className="row" style={{ gap: 10, alignItems: 'center' }}>
        <label className="small muted">Model</label>
        {opts.length > 0 ? (
          <select
            value={current}
            onChange={e => change(e.target.value)}
            disabled={busy || !loaded}
            style={{ minWidth: 280 }}
            aria-label="orchestrator model"
          >
            {!current && <option value="">— select a model —</option>}
            {opts.map(m => (
              <option key={m.id} value={m.id}>{m.label || m.id}</option>
            ))}
          </select>
        ) : (
          <span className="small muted">
            {loaded ? 'No models available — configure an endpoint above first.'
                    : 'Loading…'}
          </span>
        )}
        {busy && <span className="small muted">switching…</span>}
      </div>
    </div>
  );
}

// ── Global LLM token knobs ───────────────────────────────────────────────────
// Operator-chosen, no hardcoded constant wins over an explicit value.
// max_output_tokens = generation cap (the doer's file-write budget — too low
// truncates writes); context_window = assumed input window (escalation sizing).
function LlmSettingsCard() {
  const [out, setOut] = useState<number | ''>('');
  const [ctx, setCtx] = useState<number | ''>('');
  const [vis, setVis] = useState(false);
  const [cave, setCave] = useState(false);
  const [compactLlm, setCompactLlm] = useState(false);
  // Dynamic-context blocks — UI shows "inject X" (on); backend stores the
  // inverse ctx_no_X disable flag.
  const BLOCKS = [
    ['recall', 'Memory recall (RAG)'], ['repomap', 'Repo map'],
    ['mentions', '@-mentions'], ['summary', 'Project summary'],
    ['skills', 'Skills'], ['workflows', 'Workflows'],
  ] as const;
  const [blocks, setBlocks] = useState<Record<string, boolean>>(
    Object.fromEntries(BLOCKS.map(([k]) => [k, true])));
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.llmSettings().then(s => {
      setOut(s.max_output_tokens);
      setCtx(s.context_window);
      setVis(!!s.vision_capable);
      setCave(!!s.cave_mode);
      setCompactLlm(!!s.compact_llm);
      setBlocks(Object.fromEntries(BLOCKS.map(([k]) =>
        [k, !(s as any)[`ctx_no_${k}`]])));
    }).catch(() => { /* endpoint optional on old API */ })
      .finally(() => setLoaded(true));
  }, []);

  async function save() {
    const vals: Record<string, number> = {};
    if (typeof out === 'number') vals.max_output_tokens = out;
    if (typeof ctx === 'number') vals.context_window = ctx;
    vals.vision_capable = vis ? 1 : 0;
    vals.cave_mode = cave ? 1 : 0;
    vals.compact_llm = compactLlm ? 1 : 0;
    BLOCKS.forEach(([k]) => { vals[`ctx_no_${k}`] = blocks[k] ? 0 : 1; });
    setBusy(true);
    try {
      const s = await api.setLlmSettings(vals);
      setOut(s.max_output_tokens);
      setCtx(s.context_window);
      setVis(!!s.vision_capable);
      setCave(!!s.cave_mode);
      setCompactLlm(!!s.compact_llm);
      setBlocks(Object.fromEntries(BLOCKS.map(([k]) =>
        [k, !(s as any)[`ctx_no_${k}`]])));
      toast.success('LLM settings saved');
    } catch (e: any) {
      toast.error(`Save failed: ${e?.message || 'unknown'}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2 style={{ fontSize: 14 }}>LLM token limits</h2>
      <div className="subtitle" style={{ marginTop: 6, marginBottom: 10 }}>
        Global, applied to all agents. You choose the values — nothing is
        hardcoded over your input.
      </div>
      <div className="row" style={{ gap: 16, alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span className="small muted" title="Generation cap. Too low truncates a doer's file writes mid-string.">
            Max output tokens
          </span>
          <input
            type="number" min={256} step={1024}
            value={out} disabled={busy || !loaded}
            onChange={e => setOut(e.target.value === '' ? '' : Number(e.target.value))}
            style={{ width: 160 }}
          />
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span className="small muted" title="Assumed input context window (tokens). Match it to what the served model allows.">
            Context window (input)
          </span>
          <input
            type="number" min={1024} step={1024}
            value={ctx} disabled={busy || !loaded}
            onChange={e => setCtx(e.target.value === '' ? '' : Number(e.target.value))}
            style={{ width: 160 }}
          />
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}
               title="Fallback only — force-treat the active CHAT model as vision-capable when it isn't auto-detected. Per-agent model vision is set on each model in the Models list above; leave this off unless your chat model is multimodal but unrecognised.">
          <input type="checkbox" checked={vis} disabled={busy || !loaded}
                 onChange={e => setVis(e.target.checked)} />
          <span className="small muted">Chat model: force vision (fallback)</span>
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}
               title="Cave mode: send the agents the leanest useful context — smaller repo map, skip optional skills/workflows/@-mentions, fewer memory hits, condense sooner. Cheaper + faster on a small model; the agent can still grep/read on demand.">
          <input type="checkbox" checked={cave} disabled={busy || !loaded}
                 onChange={e => setCave(e.target.checked)} />
          <span className="small muted">🦴 Cave mode (lean context)</span>
        </label>
      </div>

      {/* Context engineering — compaction + per-turn injection knobs. */}
      <div style={{ marginTop: 16, paddingTop: 14, borderTop: '1px solid var(--border-1)' }}>
        <h3 style={{ fontSize: 13, margin: '0 0 2px' }}>Context engineering</h3>
        <div className="subtitle" style={{ marginBottom: 10 }}>
          How the agent compacts long sessions + what it injects each turn.
        </div>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10 }}
               title="On condense, summarise the dropped older turns with the model (code-aware: keeps files, symbols, decisions, errors) instead of a cheap heuristic breadcrumb. Pick the summariser model with the AIFORGE_COMPACT_ROLE env (point it at a fast/cheap model).">
          <input type="checkbox" checked={compactLlm} disabled={busy || !loaded}
                 onChange={e => setCompactLlm(e.target.checked)} />
          <span className="small muted">LLM-written, code-aware compaction (else fast heuristic)</span>
        </label>
        <div className="small muted" style={{ marginBottom: 6 }}>Inject each turn:</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px' }}>
          {BLOCKS.map(([k, label]) => (
            <label key={k} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <input type="checkbox" checked={!!blocks[k]} disabled={busy || !loaded}
                     onChange={e => setBlocks(b => ({ ...b, [k]: e.target.checked }))} />
              <span className="small muted">{label}</span>
            </label>
          ))}
        </div>
      </div>

      <div className="row" style={{ marginTop: 14 }}>
        <button className="btn" onClick={save} disabled={busy || !loaded}>
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  );
}
