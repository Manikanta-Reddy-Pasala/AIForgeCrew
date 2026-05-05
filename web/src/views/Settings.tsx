import { useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  api,
  AgentRole,
  AgentRoleConfig,
  ProviderCatalog,
  ProviderId,
  ProviderModel,
} from '../api';
import { Icon } from '../icons';

// ── per-archetype settings page ────────────────────────────────────
//
// Hits three v2 endpoints:
//   GET  /api/agents/v2/config     → { role: {provider, model, base_url} }
//   GET  /api/agents/v2/providers  → [{id, label, default_model, models:[…]}]
//   PUT  /api/agents/v2/{role}/config { provider, model, base_url? }
//
// State is local; no global store. Local + ollama_cloud catalogs are
// dynamic on the server (LM Studio /v1/models, Ollama Cloud), so a
// "Reload" button is exposed for ops to pick up freshly-downloaded
// models without a hard page refresh.

// v5 production pipeline (agents.yaml + runtime.adk_runner):
//   architect (external, human-driven)
//   → planner → verifier → LoopAgent[doer, feedback] → learner
const ROLE_ORDER: AgentRole[] = [
  'architect', 'planner', 'verifier', 'doer', 'feedback', 'learner',
];

const ROLE_HINTS: Record<AgentRole, string> = {
  architect: 'External Claude Code. Drives ticket creation; never edits code.',
  planner:   'Reads parent ticket; emits plan + child subtickets.',
  verifier:  'Plan critic. Single-turn judge BEFORE execution. Reject → re-plan.',
  doer:      'Edits code inside the subticket allowlist; runs compile + tests.',
  feedback:  'Post-execution judge. Verdict: pass | fail | scope_violation.',
  learner:   'Runs only on verdict=pass. Writes :Fact rows to memory.',
};

interface RowState {
  provider: ProviderId;
  model: string;
  base_url: string;          // empty string = use provider default
  showAdvanced: boolean;
  busy: boolean;
  justSavedAt: number | null; // epoch ms — drives the green "Saved ✓" pill
  error: string | null;
}

function emptyRow(p: ProviderId, m: string, b: string | null): RowState {
  return {
    provider: p, model: m, base_url: b ?? '',
    showAdvanced: false, busy: false, justSavedAt: null, error: null,
  };
}

// dirty = differs from the last server snapshot we successfully loaded
function isDirty(row: RowState, snap: AgentRoleConfig): boolean {
  return (
    row.provider !== snap.provider ||
    row.model !== snap.model ||
    (row.base_url || null) !== (snap.base_url || null)
  );
}

// short, human-readable context window — 128000 → "128K"
function fmtCtx(n: number | null | undefined): string {
  if (!n) return '';
  if (n >= 1000) return `${Math.round(n / 1000)}K`;
  return String(n);
}

export default function Settings() {
  const [providers, setProviders] = useState<ProviderCatalog[] | null>(null);
  const [snapshot, setSnapshot] =
    useState<Record<AgentRole, AgentRoleConfig> | null>(null);
  const [rows, setRows] = useState<Record<AgentRole, RowState>>(() =>
    Object.fromEntries(ROLE_ORDER.map(r => [r, emptyRow('local', '', null)])) as Record<AgentRole, RowState>,
  );
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);
  const [savingAll, setSavingAll] = useState(false);
  const savedTimers = useRef<Partial<Record<AgentRole, number>>>({});

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
          next[role] = emptyRow(c.provider, c.model, c.base_url);
        }
        return next;
      });
    } catch (e: any) {
      setPageError(e?.message || 'Failed to load settings');
    } finally {
      if (!silent) setLoading(false);
    }
  }

  useEffect(() => { load(); /* on mount */ }, []);
  useEffect(() => () => {
    // cleanup any pending "Saved ✓" timers on unmount
    Object.values(savedTimers.current).forEach(t => t && clearTimeout(t));
  }, []);

  // index providers by id for O(1) lookups inside render
  const provById = useMemo(() => {
    const m = new Map<ProviderId, ProviderCatalog>();
    (providers || []).forEach(p => m.set(p.id, p));
    return m;
  }, [providers]);

  function patch(role: AgentRole, next: Partial<RowState>) {
    setRows(r => ({ ...r, [role]: { ...r[role], ...next, error: null } }));
  }

  // changing provider must reset the model — the previous model id is
  // almost certainly invalid for the new provider's catalog.
  function changeProvider(role: AgentRole, providerId: ProviderId) {
    const cat = provById.get(providerId);
    const fallback = cat?.default_model || rows[role].model;
    patch(role, { provider: providerId, model: fallback });
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
      });
      // refresh snapshot for this role only — keeps dirty math accurate
      setSnapshot(s => s ? ({ ...s, [role]: {
        provider: updated.provider,
        model: updated.model,
        base_url: updated.base_url ?? null,
      } }) : s);
      patch(role, { busy: false, justSavedAt: Date.now() });
      // auto-clear the green pill after 2s
      const tid = window.setTimeout(() => {
        setRows(r => ({ ...r, [role]: { ...r[role], justSavedAt: null } }));
      }, 2000);
      const prev = savedTimers.current[role];
      if (prev) clearTimeout(prev);
      savedTimers.current[role] = tid;
      return true;
    } catch (e: any) {
      const msg = e?.message || 'save failed';
      patch(role, { busy: false, error: msg });
      // toast in addition to inline so it's visible if row is scrolled off
      toast.error(`${role}: ${msg}`);
      return false;
    }
  }

  async function saveAll() {
    if (!snapshot) return;
    const dirty = ROLE_ORDER.filter(r => isDirty(rows[r], snapshot[r]));
    if (dirty.length === 0) return;
    setSavingAll(true);
    let ok = 0, fail = 0;
    // serial so a backend rate-limit / lock can't pile up
    for (const role of dirty) {
      const success = await saveOne(role);
      success ? ok++ : fail++;
    }
    setSavingAll(false);
    if (ok && !fail) toast.success(`Saved ${ok} agent${ok === 1 ? '' : 's'}`);
    else if (ok && fail) toast.warning(`Saved ${ok}, ${fail} failed`);
  }

  // ── profile presets — bulk-apply one (provider, model) to all 9 archetypes
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
      await load(true);  // refresh table to reflect new state
    } catch (e: any) {
      toast.error(`Profile apply failed: ${e?.message || 'unknown'}`);
    } finally {
      setProfileBusy(null);
    }
  }

  // ── runtime backend toggle (separate concern, kept from prior UI)
  // This isn't part of the v2 archetype config; it flips the runtime
  // fallback chain that affects every agent. Backend options are
  // sourced from /api/runtime/llm_backend → `options` (filtered by
  // provider availability — e.g. claude_local only shows up when the
  // `claude` CLI is installed).
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
    claude_local: 'claude (subscription CLI)',
    anthropic:    'anthropic (API)',
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

  // ── header math ──
  const dirtyCount = useMemo(() => {
    if (!snapshot) return 0;
    return ROLE_ORDER.filter(r => isDirty(rows[r], snapshot[r])).length;
  }, [rows, snapshot]);

  // providers may not be loaded yet on first render (skeleton state)
  const allProviders: ProviderCatalog[] = providers || [];

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <div className="subtitle">
            Pick provider + model per archetype. Changes persist to{' '}
            <code>~/.aiforge/agent_config.json</code> and are picked up on
            the next agent invocation.
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="ghost" onClick={() => load()} disabled={loading}>
            <Icon.Refresh size={14} /> Reload
          </button>
          <button
            onClick={saveAll}
            disabled={dirtyCount === 0 || savingAll || loading}
            title={dirtyCount === 0 ? 'No unsaved changes' : `Save ${dirtyCount} row${dirtyCount === 1 ? '' : 's'}`}
          >
            {savingAll ? 'Saving…' : `Save all${dirtyCount ? ` (${dirtyCount})` : ''}`}
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

      {/* bulk profile presets — full claude_local / ollama_cloud / local */}
      {profiles.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <h2 style={{ fontSize: 14 }}>Profile preset</h2>
          <div className="subtitle" style={{ marginTop: 6, marginBottom: 10 }}>
            Bulk-assign one provider + model to all 9 archetypes. After
            applying, individual rows below can still be overridden for
            mix-and-match.
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
                {profileBusy === p.name
                  ? `Applying ${p.name}…`
                  : `Apply ${p.name}`}
                <span className="small muted" style={{ marginLeft: 6 }}>
                  ({p.provider})
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* runtime-wide backend (separate from per-archetype config) */}
      <div className="card" style={{ marginBottom: 16 }}>
        <h2 style={{ fontSize: 14 }}>LLM backend (all agents)</h2>
        <div className="subtitle" style={{ marginTop: 6, marginBottom: 10 }}>
          Default backend used by every agent when its archetype row
          below has no explicit override. Options reflect what's
          actually installed/reachable on this host (claude_local needs
          the <code>claude</code> CLI; ollama_cloud needs an API key).
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
              <option key={opt} value={opt}>
                {BACKEND_LABEL[opt] || opt}
              </option>
            ))}
          </select>
          {doerBackendBusy && <span className="small muted">switching…</span>}
        </div>
      </div>

      {/* archetype table */}
      <div className="settings-table">
        <div className="settings-table-head">
          <div>Archetype</div>
          <div>Provider</div>
          <div>Model</div>
          <div className="col-meta">Tier · Context</div>
          <div>Advanced</div>
          <div style={{ textAlign: 'right' }}>Save</div>
        </div>

        {loading || !snapshot
          ? Array.from({ length: 9 }).map((_, i) => (
              <div className="settings-row" key={`sk-${i}`}>
                <div className="role-cell">
                  <div className="skeleton" style={{ height: 14, width: '60%' }} />
                  <div className="skeleton" style={{ height: 10, width: '90%', marginTop: 6 }} />
                </div>
                <div className="skeleton" style={{ height: 30 }} />
                <div className="skeleton" style={{ height: 30 }} />
                <div className="skeleton" style={{ height: 18, width: 80 }} />
                <div className="skeleton" style={{ height: 24, width: 70 }} />
                <div className="skeleton" style={{ height: 30, width: 60 }} />
              </div>
            ))
          : ROLE_ORDER.map(role => {
              const row = rows[role];
              const snap = snapshot[role];
              const dirty = isDirty(row, snap);
              const cat = provById.get(row.provider);
              const models: ProviderModel[] = cat?.models || [];
              // current model details — handles the case where the
              // persisted model id isn't in the dynamic catalog (e.g.
              // user typed an LM Studio path that didn't probe back)
              const current = models.find(m => m.id === row.model);

              const cls = [
                'settings-row',
                `prov-${row.provider}`,
                dirty ? 'dirty' : '',
                row.justSavedAt ? 'just-saved' : '',
              ].filter(Boolean).join(' ');

              return (
                <div key={role} className={cls}>
                  <div className="role-cell">
                    <span className="role-name" title={ROLE_HINTS[role]}>
                      {role}
                      {dirty && <span className="chip sm warn">unsaved</span>}
                    </span>
                    <span className="role-hint">{ROLE_HINTS[role]}</span>
                  </div>

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

                  {/* If catalog is non-empty, present a select. If the
                      current value isn't in the catalog (e.g. dynamic
                      probe missed it), inject it as a synthetic option
                      so we don't silently switch the user's model. */}
                  {models.length > 0 ? (
                    <select
                      value={row.model}
                      onChange={e => patch(role, { model: e.target.value })}
                      disabled={row.busy}
                      aria-label={`${role} model`}
                    >
                      {!current && row.model && (
                        <option value={row.model}>{row.model} (custom)</option>
                      )}
                      {models.map(m => (
                        <option key={m.id} value={m.id}>{m.label}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      value={row.model}
                      onChange={e => patch(role, { model: e.target.value })}
                      disabled={row.busy}
                      placeholder={cat?.default_model || 'model id'}
                      aria-label={`${role} model`}
                    />
                  )}

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

                  <button
                    className={`advanced-toggle${row.showAdvanced ? ' on' : ''}`}
                    onClick={() => patch(role, { showAdvanced: !row.showAdvanced })}
                    type="button"
                  >
                    base_url
                  </button>

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

                  {row.showAdvanced && (
                    <div className="settings-advanced">
                      <label htmlFor={`baseurl-${role}`}>base_url</label>
                      <input
                        id={`baseurl-${role}`}
                        value={row.base_url}
                        onChange={e => patch(role, { base_url: e.target.value })}
                        placeholder={cat?.id === 'local'
                          ? 'http://127.0.0.1:1234/v1 (default)'
                          : '(provider default)'}
                        disabled={row.busy}
                      />
                    </div>
                  )}

                  {row.error && (
                    <div className="row-error">{row.error}</div>
                  )}
                </div>
              );
            })}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 style={{ fontSize: 14 }}>Providers</h2>
        <ul className="small muted" style={{ marginTop: 6, paddingLeft: 18 }}>
          <li><code>local</code> — mlx-lm on Mac Studio. Catalog auto-discovered from <code>http://127.0.0.1:1234/v1/models</code>.</li>
          <li><code>ollama_cloud</code> — Ollama Cloud. Requires <code>OLLAMA_CLOUD_API_KEY</code>; catalog cached for 5min.</li>
          <li><code>anthropic</code> — Claude via LiteLLM. Requires <code>ANTHROPIC_API_KEY</code>.</li>
        </ul>
      </div>
    </>
  );
}
