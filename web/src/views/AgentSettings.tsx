// Simplified agent settings: add models ONCE (with URL/key/vision), then every
// agent just picks a model by name. No per-agent URLs/keys.
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { chatApi, RegistryModel, AgentRole, AgentRoleConfig } from '../api';

const ORCH: string[] = ['enhancer', 'architect', 'planner'];
const MAIN: string[] = ['doer', 'verifier', 'feedback', 'learner', 'refiner',
                        'researcher', 'triage', 'chat'];
// Internal fan-out slots (context gatherers + verifier critics).
const ADVANCED: string[] = ['ctx_memory', 'ctx_repomap', 'ctx_conventions',
                            'verify_correctness', 'verify_scope', 'verify_risk',
                            'gap_eval', 'live_verifier'];
const ROLE_LABEL: Record<string, string> = {
  enhancer: 'Enhancer', architect: 'Architect', planner: 'Planner',
  doer: 'Doer', verifier: 'Verifier', feedback: 'Feedback', learner: 'Learner',
  refiner: 'Refiner', researcher: 'Researcher', triage: 'Triage', chat: 'Chat',
  ctx_memory: 'Ctx · memory', ctx_repomap: 'Ctx · repo map',
  ctx_conventions: 'Ctx · conventions', verify_correctness: 'Verify · correctness',
  verify_scope: 'Verify · scope', verify_risk: 'Verify · risk',
  gap_eval: 'Gap eval', live_verifier: 'Live verifier',
};

const VISION_ORDER: RegistryModel['vision'][] = ['auto', 'yes', 'no'];
function nextVision(v: RegistryModel['vision']): RegistryModel['vision'] {
  return VISION_ORDER[(VISION_ORDER.indexOf(v) + 1) % VISION_ORDER.length];
}

// Compact vision indicator. Static when `onCycle` is omitted; a click-to-cycle
// pill (auto → yes → no) otherwise — replaces the full-width <select> that used
// to dominate each model row.
function VisionBadge({ v, onCycle }: {
  v: RegistryModel['vision']; onCycle?: () => void;
}) {
  const map = { yes: ['👁 vision', '#3fb950'], no: ['🚫 no vision', '#8b949e'],
                auto: ['✨ vision: auto', '#6aa6ff'] } as const;
  const [txt, col] = map[v];
  const base = { fontSize: 11, color: col, border: `1px solid ${col}`,
                 borderRadius: 6, padding: '2px 8px', whiteSpace: 'nowrap' as const,
                 flex: '0 0 auto' as const, lineHeight: 1.4 };
  if (!onCycle) return <span style={{ ...base, marginLeft: 6 }}>{txt}</span>;
  return (
    <button type="button" onClick={onCycle}
            title="Vision support — click to cycle: auto → yes → no"
            style={{ ...base, cursor: 'pointer', background: 'transparent' }}>
      {txt}
    </button>
  );
}

const THINK_ORDER: RegistryModel['thinking'][] = ['auto', 'yes', 'no'];
function nextThinking(v: RegistryModel['thinking']): RegistryModel['thinking'] {
  return THINK_ORDER[(THINK_ORDER.indexOf(v || 'auto') + 1) % THINK_ORDER.length];
}

// Reasoning ("thinking") indicator — mirrors VisionBadge. Thinking models are
// auto-picked for planning/review roles; fast (non-thinking) coders for the doer.
function ThinkingBadge({ v, resolved, onCycle }: {
  v: RegistryModel['thinking']; resolved?: boolean; onCycle?: () => void;
}) {
  const map = { yes: ['🧠 thinking', '#d29922'], no: ['⚡ fast', '#8b949e'],
                auto: [`✨ ${resolved ? '🧠' : '⚡'} auto`, '#6aa6ff'] } as const;
  const [txt, col] = map[v || 'auto'];
  const base = { fontSize: 11, color: col, border: `1px solid ${col}`,
                 borderRadius: 6, padding: '2px 8px', whiteSpace: 'nowrap' as const,
                 flex: '0 0 auto' as const, lineHeight: 1.4 };
  if (!onCycle) return <span style={{ ...base, marginLeft: 6 }}>{txt}</span>;
  return (
    <button type="button" onClick={onCycle}
            title="Reasoning model — click to cycle: auto → yes → no. Thinking models are auto-assigned to planning/review roles."
            style={{ ...base, cursor: 'pointer', background: 'transparent' }}>
      {txt}
    </button>
  );
}

// Fallback context window (tokens) when a model has no explicit value — must
// match model_registry._CTX_STATIC_DEFAULT (128K) so the UI shows the number
// the backend actually assumes.
const DEFAULT_CTX = 131072;

// Inline per-model context-window editor. Shows the effective window in "k"
// (the model's own value, or the 128K default rendered muted). Click to edit;
// commits on blur / Enter. Blank or 0 → clears the per-model override (falls
// back to the 128K default).
function CtxEdit({ value, onCommit }: { value: number; onCommit: (tokens: number) => void }) {
  const [editing, setEditing] = useState(false);
  const isDefault = !value || value <= 0;
  const shownK = Math.round((value || DEFAULT_CTX) / 1000);
  const [draft, setDraft] = useState(String(shownK));
  const commit = () => {
    setEditing(false);
    const k = Number(draft);
    if (!Number.isFinite(k) || k <= 0) { onCommit(0); return; }   // clear → default
    onCommit(Math.round(k * 1000));
  };
  const base = { fontSize: 11, borderRadius: 6, padding: '2px 8px',
                 whiteSpace: 'nowrap' as const, flex: '0 0 auto' as const, lineHeight: 1.4 };
  if (editing) {
    return (
      <input autoFocus type="number" min={0} step={8}
             value={draft} onChange={e => setDraft(e.target.value)}
             onBlur={commit}
             onKeyDown={e => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') setEditing(false); }}
             title="Context window in thousands of tokens (K). Blank/0 = 128K default."
             style={{ ...base, width: 64, border: '1px solid #6aa6ff' }} />
    );
  }
  const col = isDefault ? '#8b949e' : '#6aa6ff';
  return (
    <button type="button"
            onClick={() => { setDraft(String(shownK)); setEditing(true); }}
            title={isDefault
              ? 'Context window: 128K default (click to set a per-model value)'
              : 'Per-model context window — click to edit (blank/0 = 128K default)'}
            style={{ ...base, color: col, border: `1px solid ${col}`,
                     cursor: 'pointer', background: 'transparent' }}>
      ctx {shownK}k{isDefault ? ' ·default' : ''}
    </button>
  );
}

export default function AgentSettings() {
  const [models, setModels] = useState<RegistryModel[]>([]);
  const [config, setConfig] = useState<Record<string, AgentRoleConfig>>({});
  // Optimistic role→modelId map. apply() sets it immediately so the dropdown
  // reflects the choice instantly — and keeps reflecting it after reload even
  // when the backend rewrites base_url (set_role nulls an empty url, load_all
  // then inherits the global default), which used to break the model+url
  // equality match and snap the select back to "— pick a model —".
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [autoBusy, setAutoBusy] = useState(false);

  async function loadModels() {
    try { setModels((await chatApi.models()).models); } catch { /* */ }
  }

  // Capability-based auto-assignment: the backend chooses each agent's model by
  // capability (reasoning→planning/review, fast coder→doer, vision where needed).
  async function autoAssignAll() {
    setAutoBusy(true);
    try {
      const r = await chatApi.autoAssign();
      const n = Object.keys(r.assignments || {}).length;
      setPicked(r.assignments || {});
      toast.success(`Auto-assigned ${n} agents by capability`);
      await loadConfig();
    } catch (e: any) {
      toast.error(e?.message || 'Auto-assign failed');
    } finally {
      setAutoBusy(false);
    }
  }
  async function loadConfig() {
    try { setConfig(await chatApi.agentsV2Config() as any); } catch { /* */ }
  }
  useEffect(() => { loadModels(); loadConfig(); }, []);

  // Other agents = MAIN + ADVANCED, plus any extra configured role not already
  // listed. Rendered from static lists so every agent shows even before it has
  // explicit saved config (the v2 config only returns explicitly-set roles).
  const known = new Set([...ORCH, ...MAIN, ...ADVANCED]);
  const extraRoles = Object.keys(config).filter(r => !known.has(r));
  const otherRoles = [...MAIN, ...ADVANCED, ...extraRoles];

  // Which registry model is a role currently pointed at. Resolution order:
  //   1. optimistic pick from the last apply() (survives the base_url rewrite)
  //   2. exact match on model id + base_url
  //   3. lenient match on model id alone (handles the inherited-url case where
  //      the saved row's base_url no longer equals the registry row's)
  function selectedId(role: string): string {
    if (picked[role]) return picked[role];
    const c = config[role];
    if (!c) return '';
    const exact = models.find(x => x.model === c.model &&
      (x.base_url || '') === (c.base_url || ''));
    if (exact) return exact.id;
    const byModel = models.find(x => x.model === c.model);
    return byModel?.id || '';
  }

  async function apply(modelId: string, roles: string[]) {
    if (!modelId) return;
    // Reflect the choice instantly for every targeted role.
    setPicked(p => { const n = { ...p }; roles.forEach(r => { n[r] = modelId; }); return n; });
    try {
      const r = await chatApi.applyModel(modelId, roles);
      await loadConfig();
      const m = models.find(x => x.id === modelId);
      toast.success(`${m?.label || 'Model'} → ${r.applied.length} agent${r.applied.length === 1 ? '' : 's'}`);
    } catch (e: any) {
      // Roll back the optimistic pick on failure.
      setPicked(p => { const n = { ...p }; roles.forEach(r => { delete n[r]; }); return n; });
      toast.error(`Apply failed: ${e.message}`);
    }
  }

  // Read-only: the system auto-decides each agent's model by capability whenever
  // the model set changes. This just SHOWS the decision + capabilities.
  function RoleRow({ role }: { role: string }) {
    const sel = selectedId(role);
    const m = models.find(x => x.id === sel);
    const name = m?.label || config[role]?.model || '—';
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0' }}>
        <span style={{ width: 130, fontSize: 13 }}>{ROLE_LABEL[role] || role}</span>
        <span style={{ flex: 1, maxWidth: 280, fontSize: 13, color: m ? '#c9d1d9' : '#8b949e' }}
              title={m?.model || ''}>{name}</span>
        {m && <ThinkingBadge v={m.thinking} resolved={m.has_thinking} />}
        {m && <VisionBadge v={m.vision} />}
      </div>
    );
  }

  return (
    <>
      <ModelsCard models={models} reload={loadModels} />

      <ApprovalsCard />

      <div className="card" style={{ marginBottom: 16 }}>
        <h2 style={{ fontSize: 14 }}>Agents <span className="small muted">· auto-decided</span></h2>
        <div className="subtitle" style={{ marginTop: 4, marginBottom: 12 }}>
          The system chooses each agent's model automatically by capability —
          🧠 reasoning models handle planning/review, ⚡ fast coders do the code,
          👁 vision models where needed. Add or remove a model and everything
          re-assigns. Token limits + context window are automatic too.
        </div>

        <h3 style={{ fontSize: 13, margin: '8px 0' }}>Orchestrator</h3>
        {ORCH.map(r => <RoleRow key={r} role={r} />)}

        <h3 style={{ fontSize: 13, margin: '16px 0 8px' }}>Other agents</h3>
        {otherRoles.map(r => <RoleRow key={r} role={r} />)}
      </div>
    </>
  );
}

// Per-chat-mode approval toggle. ON = that mode pauses for human Approve/Reject
// on ask-policy / review-gated tool calls; OFF = the run proceeds without
// interruption (a hard DENY policy and a destructive-delete confirm still
// apply — those aren't chat-mode approvals). Autonomous ticket runs unaffected.
const APPROVAL_MODES: { key: 'chat' | 'plan' | 'pipeline'; label: string; hint: string }[] = [
  { key: 'chat', label: 'Chat', hint: 'quick single-turn answers' },
  { key: 'plan', label: 'Plan', hint: 'planning / analysis' },
  { key: 'pipeline', label: 'Pipeline', hint: 'multi-agent build (team)' },
];

function ApprovalsCard() {
  const [flags, setFlags] = useState<{ chat: boolean; plan: boolean; pipeline: boolean }>(
    { chat: true, plan: true, pipeline: true });

  useEffect(() => {
    chatApi.approvalSettings().then(setFlags).catch(() => { /* */ });
  }, []);

  async function toggle(mode: 'chat' | 'plan' | 'pipeline', enabled: boolean) {
    const prev = flags;
    setFlags(f => ({ ...f, [mode]: enabled }));    // optimistic
    try {
      setFlags(await chatApi.setApprovalMode(mode, enabled));
      toast.success(`${mode} approvals ${enabled ? 'on' : 'off'}`);
    } catch (e: any) {
      setFlags(prev);
      toast.error(e?.message || 'Failed to update approvals');
    }
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2 style={{ fontSize: 14 }}>Approvals <span className="small muted">· per chat mode</span></h2>
      <div className="subtitle" style={{ marginTop: 4, marginBottom: 12 }}>
        When ON, the mode pauses for your Approve/Reject before risky or
        file-changing tool calls. Turn OFF to let a mode run uninterrupted.
        Denied-by-policy actions and destructive deletes still confirm.
      </div>
      {APPROVAL_MODES.map(({ key, label, hint }) => (
        <label key={key} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', cursor: 'pointer' }}>
          <input type="checkbox" checked={flags[key]}
                 onChange={e => toggle(key, e.target.checked)} />
          <span style={{ width: 90, fontSize: 13 }}>{label}</span>
          <span style={{ fontSize: 12, color: '#8b949e' }}>{hint}</span>
        </label>
      ))}
    </div>
  );
}

function ModelsCard({ models, reload }: { models: RegistryModel[]; reload: () => void }) {
  const [label, setLabel] = useState('');
  const [model, setModel] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [ctx, setCtx] = useState<number | ''>('');
  const [vision, setVision] = useState<'auto' | 'yes' | 'no'>('auto');
  const [busy, setBusy] = useState(false);
  const [syncing, setSyncing] = useState(false);
  // Model discovery from the typed Base URL (GET {base_url}/models).
  const [identifying, setIdentifying] = useState(false);
  const [discovered, setDiscovered] = useState<string[]>([]);
  const [loadingId, setLoadingId] = useState('');
  const input = { width: '100%', padding: '6px 8px', fontSize: 13 };

  async function add() {
    if (!model.trim()) { toast.error('Model id is required'); return; }
    setBusy(true);
    try {
      await chatApi.addModel({ label: label.trim() || model.trim(), model: model.trim(),
        base_url: baseUrl.trim(), api_key: apiKey.trim() || undefined,
        vision, context_window: typeof ctx === 'number' ? ctx : 0 });
      setLabel(''); setModel(''); setCtx('');
      reload();
      toast.success('Model added');
    } catch (e: any) { toast.error(`Add failed: ${e.message}`); }
    finally { setBusy(false); }
  }
  // Probe the Base URL and list its models so the user picks the ones to add
  // (instead of typing each model id by hand). This is the real "identify"
  // flow — it actually contacts the server URL, unlike "Detect current models"
  // which only mirrors what the agents are already configured with.
  async function identify() {
    if (!baseUrl.trim()) { toast.error('Enter a Base URL first'); return; }
    setIdentifying(true); setDiscovered([]);
    try {
      const r = await chatApi.providersTest(baseUrl.trim(), apiKey.trim() || undefined, true);
      if (!r.ok) { toast.error(`Could not reach server: ${r.error || 'unknown error'}`); return; }
      const ids = r.models || [];
      setDiscovered(ids);
      toast.success(ids.length ? `Found ${ids.length} model${ids.length === 1 ? '' : 's'}` : 'Server returned no models');
    } catch (e: any) { toast.error(e.message); }
    finally { setIdentifying(false); }
  }
  // Add one discovered model id to the registry (with the current URL/key/vision).
  async function addDiscovered(id: string) {
    try {
      await chatApi.addModel({ label: id.split('/').pop() || id, model: id,
        base_url: baseUrl.trim(), api_key: apiKey.trim() || undefined,
        vision, context_window: typeof ctx === 'number' ? ctx : 0 });
      setDiscovered(d => d.filter(x => x !== id));
      reload();
      toast.success(`Added ${id}`);
    } catch (e: any) { toast.error(`Add failed: ${e.message}`); }
  }
  async function setVisionFor(id: string, v: RegistryModel['vision']) {
    try { await chatApi.updateModel(id, { vision: v }); reload(); } catch (e: any) { toast.error(e.message); }
  }
  async function setThinkingFor(id: string, v: RegistryModel['thinking']) {
    try { await chatApi.updateModel(id, { thinking: v }); reload(); } catch (e: any) { toast.error(e.message); }
  }
  // Per-model context window (tokens). 0/blank = fall back to the 128K default.
  async function setContextFor(id: string, tokens: number) {
    try {
      await chatApi.updateModel(id, { context_window: Math.max(0, tokens | 0) });
      reload();
    } catch (e: any) { toast.error(e.message); }
  }
  async function loadOnServer(m: RegistryModel) {
    setLoadingId(m.id);
    try {
      const r = await chatApi.reloadModel(m.model, m.context_window || 0);
      toast.success(`Loaded ${m.model} @ ${Math.round((r.context_length || 0) / 1000)}k ctx`);
    } catch (e: any) {
      // Surface the real reason instead of a silent no-op (e.g. 503 when no
      // LM Studio host is configured — loading only applies to an LMS server).
      toast.error(`Load failed: ${e.message}`);
    } finally { setLoadingId(''); }
  }
  async function del(id: string) {
    if (!window.confirm('Remove this model?')) return;
    try { await chatApi.deleteModel(id); reload(); } catch (e: any) { toast.error(e.message); }
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2 style={{ fontSize: 14 }}>Models</h2>
      <div className="subtitle" style={{ marginTop: 4, marginBottom: 12 }}>
        Add the OpenAI-compatible models you use (URL + optional key). Agents
        below just pick one by name.
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, maxWidth: 640 }}>
        <label className="small">Name<input style={input} value={label} placeholder="Qwen Coder"
               onChange={e => setLabel(e.target.value)} /></label>
        <label className="small">Model id<input style={input} value={model} placeholder="qwen3-coder-next"
               onChange={e => setModel(e.target.value)} /></label>
        <label className="small">Base URL<input style={input} value={baseUrl} placeholder="http://host:1234/v1"
               onChange={e => setBaseUrl(e.target.value)} /></label>
        <label className="small">API key <span className="muted">(optional)</span>
          <input style={input} type="password" value={apiKey} placeholder="leave blank for none"
                 onChange={e => setApiKey(e.target.value)} /></label>
        <label className="small">Vision
          <select style={input} value={vision} onChange={e => setVision(e.target.value as any)}>
            <option value="auto">Auto-detect</option>
            <option value="yes">Yes — supports images</option>
            <option value="no">No</option>
          </select></label>
        <label className="small">Context window <span className="muted">(tokens, optional)</span>
          <input style={input} type="number" min={0} step={1024} value={ctx}
                 placeholder="blank = 128K default"
                 onChange={e => setCtx(e.target.value === '' ? '' : Number(e.target.value))} /></label>
      </div>
      <div className="xs muted" style={{ marginTop: 6 }}>TLS verification is skipped for these endpoints (self-hosted / self-signed).</div>
      <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
        <button className="btn" onClick={add} disabled={busy}>
          {busy ? 'Adding…' : '+ Add model'}
        </button>
        <button className="ghost" disabled={identifying || !baseUrl.trim()}
                title="Contact the Base URL and list the models it serves"
                onClick={identify}>
          {identifying ? 'Identifying…' : '🔍 Identify models from URL'}
        </button>
        <button className="ghost" disabled={busy || syncing}
                title="Populate this list from the models the agents are already configured with"
                onClick={async () => {
                  setSyncing(true);
                  try { const r = await chatApi.syncModels(); reload();
                    toast.success(r.count ? `Detected ${r.count} model${r.count === 1 ? '' : 's'}` : 'No new models found'); }
                  catch (e: any) { toast.error(e.message); }
                  finally { setSyncing(false); }
                }}>
          {syncing ? 'Detecting…' : '⟳ Detect current models'}
        </button>
      </div>

      {discovered.length > 0 && (
        <div style={{ marginTop: 10, padding: 10, border: '1px dashed var(--border-1)',
                      borderRadius: 8 }}>
          <div className="small muted" style={{ marginBottom: 6 }}>
            Models served at <b>{baseUrl.trim()}</b> — click to add (uses the
            URL / key / vision above):
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {discovered.map(id => (
              <button key={id} className="ghost sm" onClick={() => addDiscovered(id)}
                      style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                + {id}
              </button>
            ))}
          </div>
        </div>
      )}

      {models.length > 0 && (
        <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {models.map(m => (
            <div key={m.id} style={{ display: 'flex', alignItems: 'center', gap: 10,
                                     padding: '6px 8px', border: '1px solid var(--border-1)', borderRadius: 6 }}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap',
                              overflow: 'hidden', textOverflow: 'ellipsis' }}>{m.label}</div>
                <div className="xs muted" style={{ fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {m.model} · {m.base_url || 'default url'}{m.api_key_set ? ' · 🔑' : ''}
                </div>
              </div>
              <CtxEdit value={m.context_window} onCommit={t => setContextFor(m.id, t)} />
              <ThinkingBadge v={m.thinking} resolved={m.has_thinking}
                             onCycle={() => setThinkingFor(m.id, nextThinking(m.thinking))} />
              <VisionBadge v={m.vision} onCycle={() => setVisionFor(m.id, nextVision(m.vision))} />
              <button className="ghost sm" disabled={loadingId === m.id}
                      title="Load this model on the LM Studio host (no-op for cloud / non-LMS backends)"
                      onClick={() => loadOnServer(m)}>
                {loadingId === m.id ? '…' : '⏏ Load'}
              </button>
              <button className="ghost sm" style={{ color: 'var(--err)' }} onClick={() => del(m.id)}>✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
