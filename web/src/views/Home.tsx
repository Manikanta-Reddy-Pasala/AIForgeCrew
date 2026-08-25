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
  LlmSettings,
  LlmSettingsInput,
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
        const modelPlural = modelCount === 1 ? '' : 's';
        const msg = modelCount > 0
          ? `Reachable — ${modelCount} model${modelPlural}`
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
        const nPlural = n === 1 ? '' : 's';
        toast.success(n > 0 ? `Reachable — ${n} model${nPlural}` : 'Reachable');
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
            <button type="button" className="ghost" onClick={() => load()} disabled={loading}>
              <Icon.Refresh size={14} /> Reload
            </button>
            <button type="button"
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
          <button type="button" className="ghost sm" onClick={() => load()}>
            Retry
          </button>
        </div>
      )}

      {/* ── Settings tabs: Agent | Integrations ──────────────────── */}
      <div className="row" style={{ gap: 4, marginBottom: 16, borderBottom: '1px solid var(--border-1)' }}>
        {(['agent', 'integrations'] as const).map(t => (
          <button type="button" key={t} onClick={() => setTab(t)}
                  className={tab === t ? '' : 'ghost'}
                  style={{ borderRadius: '6px 6px 0 0', textTransform: 'capitalize',
                           fontWeight: tab === t ? 600 : 400 }}>
            {t}
          </button>
        ))}
      </div>

      {tab === 'integrations' && <IntegrationsTab />}

      {tab === 'agent' && (<>
        {/* Simplified: add models once — the system auto-decides everything
            (agent→model by capability, token limits, context window). */}
        <AgentSettings />
        <AgentLimitsCard />
      </>)}

    </>
  );
}

// Integrations tab — pick Jira / Confluence / GitLab / Email, configure one at a time.
function IntegrationsTab() {
  const [sub, setSub] = useState<'jira' | 'confluence' | 'gitlab' | 'email'>('jira');
  const LABELS: Record<string, string> = { gitlab: 'GitLab', email: 'Email' };
  const label = (s: string) => LABELS[s] ?? s;
  return (
    <div className="card">
      <div className="row" style={{ gap: 4, marginBottom: 12 }}>
        {(['jira', 'confluence', 'gitlab', 'email'] as const).map(s => (
          <button type="button" key={s} onClick={() => setSub(s)}
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

// ── Per-turn agent limits ────────────────────────────────────────────────────
// The step cap and the turn deadline are RUNAWAY guards, not task budgets. A
// turn that keeps producing new work extends them by itself (auto-extensions)
// instead of dying with its work thrown away — these knobs let the operator
// size all three. Persisted via /runtime/llm-settings (~/.aiforge/
// runtime_settings.json); the matching env vars still work headless.
function AgentLimitsCard() {
  const [vals, setVals] = useState<Partial<LlmSettings>>({});
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  // Bumped to force the uncontrolled inputs to remount — after a rejected edit
  // they must snap back to what the server actually holds, not keep showing the
  // value it refused.
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    api.llmSettings()
      // loaded ONLY on success: a failed load leaves the fields empty, and an
      // empty ENABLED field is one stray blur away from writing a 0.
      .then(s => { setVals(s); setLoaded(true); })
      .catch(() => { /* older build without the endpoint — card stays inert */ });
  }, []);

  // One click for "stop guarding me": both runaway guards to 0. Typing
  // 1000000 in one field and 0 in the other is the same intent expressed
  // badly, and a step cap has no obvious "off" number to guess at.
  async function noLimits() {
    setBusy(true);
    try {
      const next = await api.setLlmSettings({
        // The rate ceiling too: it is the likeliest reason a turn LOOKS stuck,
        // so a button whose tooltip says "nothing stops a turn but you" cannot
        // leave a 5/min throttle in force.
        chat_safety_cap: 0, chat_turn_deadline_s: 0, llm_max_rpm: 0,
        compaction_rpm: 0, chat_rpm: 0,
        llm_rate_limit_backoff_s: 20, llm_rate_limit_cap_s: 60,
      } as LlmSettingsInput);
      setVals(next);
      setNonce(x => x + 1);
      toast.success('Agent limits off — no step cap, no deadline, no rate ceiling');
    } catch (e: any) {
      toast.error(`Save failed: ${e?.message || 'unknown'}`);
      setNonce(x => x + 1);
    } finally {
      setBusy(false);
    }
  }

  async function resetAll() {
    setBusy(true);
    try {
      const next = await api.setLlmSettings({
        unset: ['chat_safety_cap', 'chat_turn_deadline_s', 'chat_cap_extensions',
                'chat_unattended_cap', 'llm_max_rpm',
                'compaction_rpm', 'chat_rpm',
                'llm_rate_limit_backoff_s', 'llm_rate_limit_cap_s'],
      });
      setVals(next);
      setNonce(x => x + 1);
      toast.success('Agent limits reset to defaults');
    } catch (e: any) {
      toast.error(`Reset failed: ${e?.message || 'unknown'}`);
    } finally {
      setBusy(false);
    }
  }

  async function commit(key: keyof LlmSettings, raw: string, lo: number, hi: number) {
    // An EMPTY field is "I did not mean to change this", never 0 — a browser
    // reports '' for anything it cannot parse, and Number('') is 0, which for
    // the three lo=0 knobs would silently disable the guard and report success.
    if (raw.trim() === '') { setNonce(x => x + 1); return; }
    const n = Number(raw);
    if (!Number.isFinite(n) || !Number.isInteger(n) || n < lo || n > hi) {
      toast.error(`${key} must be a whole number between ${lo} and ${hi}`);
      setNonce(x => x + 1);            // snap the field back to the stored value
      return;
    }
    // Same value, different spelling ("02000", "2000.0") — nothing to save,
    // but the field must still snap back to the stored form.
    if (n === vals[key]) { setNonce(x => x + 1); return; }
    setBusy(true);
    try {
      const next = await api.setLlmSettings({ [key]: n } as LlmSettingsInput);
      setVals(next);
      setNonce(x => x + 1);
      toast.success('Agent limits saved');
    } catch (e: any) {
      toast.error(`Save failed: ${e?.message || 'unknown'}`);
      setNonce(x => x + 1);            // server state is unchanged — show that
    } finally {
      setBusy(false);
    }
  }

  const field = (
    key: keyof LlmSettings, label: string, hint: string, lo: number, hi: number,
  ) => (
    <label className="small" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <span>{label}</span>
      <input
        type="number" min={lo} max={hi} step={1}
        defaultValue={vals[key] ?? ''}
        key={`${String(key)}-${nonce}-${vals[key] ?? ''}`}
        disabled={!loaded || busy}
        onBlur={e => commit(key, e.target.value, lo, hi)}
        onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
        style={{ width: 140 }}
        aria-label={label}
      />
      <span className="xs muted" style={{ maxWidth: 260 }}>{hint}</span>
    </label>
  );

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ fontSize: 14 }}>Agent limits</h2>
        <div className="row" style={{ gap: 8 }}>
        <button type="button" className="ghost sm" onClick={noLimits} disabled={!loaded || busy}
                title="Step cap and turn deadline both to 0 — nothing stops a turn but the agent itself, a stall, or Stop">
          ∞ No limits
        </button>
        <button type="button" className="ghost sm" onClick={resetAll} disabled={!loaded || busy}
                title="Forget the saved values so the env vars / built-in defaults apply again">
          ↺ Reset to defaults
        </button>
        </div>
      </div>
      <div className="subtitle" style={{ marginTop: 6, marginBottom: 10 }}>
        Runaway guards for a single chat turn. When a turn hits one but is still
        producing new work — it edited a file, or read something it had not read
        before — it condenses its history and extends itself instead of stopping.
        A turn that is only spinning is stopped either way. 0 in either field
        removes that guard entirely — with both off, only the agent finishing or
        your Stop reliably ends a turn (the stall guards catch exact repeats
        only, so an agent that keeps varying its arguments will run until you
        stop it). Background runs nobody is watching keep a cap regardless.
        Saving here overrides the matching env var; “Reset to defaults” hands
        control back to it.
      </div>
      <div className="row" style={{ gap: 24, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        {field('chat_safety_cap', 'Step cap',
               'Tool/model steps in one turn before the runaway guard fires. 0 = no limit, which is the DEFAULT.',
               0, 1_000_000)}
        {field('chat_turn_deadline_s', 'Turn deadline (seconds)',
               'Wall clock for one turn. 0 = no deadline, which is the DEFAULT.',
               0, 86_400)}
        {field('chat_cap_extensions', 'Auto-extensions',
               'How many times a turn still making progress may extend the cap / deadline. 0 = stop hard. Default 2.',
               0, 50)}
        {field('llm_max_rpm', 'LLM calls per minute (global)',
               'Ceiling on model requests in any 60 seconds, shared by EVERY AIForge process on this machine — chat, the routers, jobs, the team-pipeline runner and memory\u2019s structured extractions (not embeddings, which go to a local sidecar). 0 = no ceiling. Set it BELOW your provider\u2019s published limit, not at it. One agent turn is routinely 10-40 calls and the memory fold runs alongside it, so a low value queues ordinary work for minutes. Calls WAIT, they are never failed; the toolbar shows \u23f3 while any are parked, and says whether the ceiling is machine-wide or has fallen back to this process alone.',
               0, 100_000)}
        {field('compaction_rpm', 'Compaction calls per minute',
               'Sub-ceiling for MEMORY / compaction LLM calls only - the background folding (okf tiers, note consolidation, the boot fold) on the "learner" role. Kept small so background distillation can never crowd out your chat or flood the provider. 0 = bounded only by the global ceiling above. Default 5. To stop compaction from calling the model AT ALL, use the Disable-compaction toggle instead.',
               0, 100_000)}
        {field('chat_rpm', 'Chat / other calls per minute',
               'Sub-ceiling for chat and every OTHER (non-compaction) LLM call. 0 = bounded only by the global ceiling above. Default 15. Compaction + chat are independent buckets that together fit under the global cap (5 + 15 = 20).',
               0, 100_000)}
        {field('llm_rate_limit_backoff_s', 'Rate-limit backoff (s)',
               'How long to wait after the PROVIDER rejects us for sending too fast and sends no Retry-After. It is counting a minute, so a sub-second backoff just re-earns the rejection and pays a request to do it. 0 = ordinary exponential backoff. Default 20.',
               0, 3_600)}
        {field('llm_rate_limit_cap_s', 'Rate-limit cap (s)',
               'The most a SINGLE rejection may cost — both this caller\u2019s own wait and the hold every other caller then observes. Retry-After is a number the remote server chose, and unbounded it lets one header park the whole box. Default 60.',
               1, 3_600)}
        {field('chat_unattended_cap', 'Background step cap',
               'Steps for runs with nobody watching (scheduled jobs, analysis fan-out, subtasks). These have no Stop button, so this one cannot be 0. Default 2000.',
               1, 1_000_000)}
      </div>
    </div>
  );
}

