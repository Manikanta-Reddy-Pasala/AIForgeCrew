// Simplified agent settings: add models ONCE (with URL/key/vision), then every
// agent just picks a model by name. No per-agent URLs/keys.
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { chatApi, RegistryModel, AgentRole, AgentRoleConfig } from '../api';

const ORCH: string[] = ['enhancer', 'architect', 'planner'];
const MAIN: string[] = ['doer', 'verifier', 'feedback', 'learner', 'refiner',
                        'researcher', 'triage', 'chat'];
// Internal fan-out slots — shown under an "Advanced" fold.
const ROLE_LABEL: Record<string, string> = {
  enhancer: 'Enhancer', architect: 'Architect', planner: 'Planner',
  doer: 'Doer', verifier: 'Verifier', feedback: 'Feedback', learner: 'Learner',
  refiner: 'Refiner', researcher: 'Researcher', triage: 'Triage', chat: 'Chat',
};

function VisionBadge({ v }: { v: RegistryModel['vision'] }) {
  const map = { yes: ['👁 vision', '#3fb950'], no: ['no vision', '#8b949e'],
                auto: ['auto-detect', '#6aa6ff'] } as const;
  const [txt, col] = map[v];
  return <span style={{ fontSize: 10, color: col, border: `1px solid ${col}`,
                        borderRadius: 4, padding: '0 4px', marginLeft: 6 }}>{txt}</span>;
}

export default function AgentSettings() {
  const [models, setModels] = useState<RegistryModel[]>([]);
  const [config, setConfig] = useState<Record<string, AgentRoleConfig>>({});

  async function loadModels() {
    try { setModels((await chatApi.models()).models); } catch { /* */ }
  }
  async function loadConfig() {
    try { setConfig(await chatApi.agentsV2Config() as any); } catch { /* */ }
  }
  useEffect(() => { loadModels(); loadConfig(); }, []);

  const allRoles = Object.keys(config);
  const advRoles = allRoles.filter(r => !ORCH.includes(r) && !MAIN.includes(r));

  // Which registry model is a role currently pointed at (match model + url).
  function selectedId(role: string): string {
    const c = config[role];
    if (!c) return '';
    const m = models.find(x => x.model === c.model &&
      (!x.base_url || x.base_url === (c.base_url || '')));
    return m?.id || '';
  }

  async function apply(modelId: string, roles: string[]) {
    if (!modelId) return;
    try {
      const r = await chatApi.applyModel(modelId, roles);
      await loadConfig();
      const m = models.find(x => x.id === modelId);
      toast.success(`${m?.label || 'Model'} → ${r.applied.length} agent${r.applied.length === 1 ? '' : 's'}`);
    } catch (e: any) { toast.error(`Apply failed: ${e.message}`); }
  }

  function RolePicker({ role }: { role: string }) {
    const sel = selectedId(role);
    const m = models.find(x => x.id === sel);
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0' }}>
        <span style={{ width: 110, fontSize: 13 }}>{ROLE_LABEL[role] || role}</span>
        <select value={sel} disabled={!models.length}
                onChange={e => apply(e.target.value, [role])}
                style={{ flex: 1, maxWidth: 280, fontSize: 13 }}>
          <option value="">{models.length ? '— pick a model —' : '(add a model first)'}</option>
          {models.map(x => <option key={x.id} value={x.id}>{x.label}</option>)}
        </select>
        {m && <VisionBadge v={m.vision} />}
        {!m && config[role]?.model && (
          <span className="xs muted" title={config[role].base_url || ''}>{config[role].model}</span>
        )}
      </div>
    );
  }

  function GroupApply({ roles, label }: { roles: string[]; label: string }) {
    const [pick, setPick] = useState('');
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <span className="small muted" style={{ width: 110 }}>{label}</span>
        <select value={pick} disabled={!models.length}
                onChange={e => { setPick(e.target.value); apply(e.target.value, roles); setPick(''); }}
                style={{ flex: 1, maxWidth: 280, fontSize: 13 }}>
          <option value="">apply one model to all…</option>
          {models.map(x => <option key={x.id} value={x.id}>{x.label}</option>)}
        </select>
      </div>
    );
  }

  return (
    <>
      <ModelsCard models={models} reload={loadModels} />

      <div className="card" style={{ marginBottom: 16 }}>
        <h2 style={{ fontSize: 14 }}>Agents</h2>
        <div className="subtitle" style={{ marginTop: 4, marginBottom: 12 }}>
          Pick a model for each agent — bulk per group, or individually. Vision
          support is shown next to the model.
        </div>

        <h3 style={{ fontSize: 13, margin: '8px 0' }}>Orchestrator</h3>
        <GroupApply roles={ORCH} label="all 3 →" />
        {ORCH.map(r => <RolePicker key={r} role={r} />)}

        <h3 style={{ fontSize: 13, margin: '16px 0 8px' }}>Other agents</h3>
        <GroupApply roles={[...MAIN, ...advRoles]} label="all →" />
        {/* Every other agent listed individually with its own model selector. */}
        {[...MAIN.filter(r => allRoles.includes(r)), ...advRoles].map(r =>
          <RolePicker key={r} role={r} />)}
      </div>
    </>
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
  const input = { width: '100%', padding: '6px 8px', fontSize: 13 };

  async function add() {
    if (!model.trim()) { toast.error('Model id is required'); return; }
    setBusy(true);
    try {
      await chatApi.addModel({ label: label.trim() || model.trim(), model: model.trim(),
        base_url: baseUrl.trim(), api_key: apiKey.trim() || undefined,
        vision, context_window: typeof ctx === 'number' ? ctx : 0 });
      setLabel(''); setModel(''); setBaseUrl(''); setApiKey(''); setCtx(''); setVision('auto');
      reload();
      toast.success('Model added');
    } catch (e: any) { toast.error(`Add failed: ${e.message}`); }
    finally { setBusy(false); }
  }
  async function setVisionFor(id: string, v: RegistryModel['vision']) {
    try { await chatApi.updateModel(id, { vision: v }); reload(); } catch (e: any) { toast.error(e.message); }
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
                 placeholder="blank = use global"
                 onChange={e => setCtx(e.target.value === '' ? '' : Number(e.target.value))} /></label>
      </div>
      <div className="xs muted" style={{ marginTop: 6 }}>TLS verification is skipped for these endpoints (self-hosted / self-signed).</div>
      <button className="btn" onClick={add} disabled={busy} style={{ marginTop: 10 }}>
        {busy ? 'Adding…' : '+ Add model'}
      </button>

      {models.length > 0 && (
        <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {models.map(m => (
            <div key={m.id} style={{ display: 'flex', alignItems: 'center', gap: 10,
                                     padding: '6px 8px', border: '1px solid var(--border-1)', borderRadius: 6 }}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{m.label} <VisionBadge v={m.vision} /></div>
                <div className="xs muted" style={{ fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {m.model} · {m.base_url || 'default url'}{m.api_key_set ? ' · 🔑' : ''}{m.context_window ? ` · ctx ${Math.round(m.context_window / 1000)}k` : ''}
                </div>
              </div>
              <select value={m.vision} onChange={e => setVisionFor(m.id, e.target.value as any)}
                      title="Vision support" style={{ fontSize: 12 }}>
                <option value="auto">vision: auto</option>
                <option value="yes">vision: yes</option>
                <option value="no">vision: no</option>
              </select>
              <button className="ghost sm" style={{ color: 'var(--err)' }} onClick={() => del(m.id)}>✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
