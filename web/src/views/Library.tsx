import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api } from '../api';
import { Icon } from '../icons';
import { MdLite } from '../mdlite';
import { clickable } from '../a11y';

type Kind = 'skills' | 'workflows' | 'rules';

const LABEL: Record<Kind, { title: string; one: string; blurb: string }> = {
  skills:    { title: 'Skills',    one: 'skill',    blurb: 'Reusable know-how the agents pull in automatically when a task matches the triggers.' },
  workflows: { title: 'Workflows', one: 'workflow', blurb: 'End-to-end procedures (ship a feature, fix a bug, run & demo an app) the agents follow step by step.' },
  rules:     { title: 'Rules',     one: 'rule',     blurb: 'Always-on coding constraints the agents must obey. Memory, chats and tickets are NOT affected here.' },
};

export default function Library({ kind }: { kind: Kind }) {
  const qc = useQueryClient();
  const meta = LABEL[kind];
  const navigate = useNavigate();
  const { data: items = [], isLoading } = useQuery({
    queryKey: ['library', kind],
    queryFn: () => api.libraryList(kind),
  });

  const [tab, setTab] = useState<'default' | 'custom'>('default');
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [triggers, setTriggers] = useState('');
  const [body, setBody] = useState('');
  const [genPrompt, setGenPrompt] = useState('');
  const [generating, setGenerating] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  // Default = bundled/built-in/repo items; Custom = user-created (origin from API).
  const shown = (items as any[]).filter(it => (it.origin ?? 'default') === tab);

  function reset() {
    setName(''); setDescription(''); setTriggers(''); setBody(''); setGenPrompt('');
  }

  async function generate() {
    if (!genPrompt.trim()) { toast.error('Describe what you want first'); return; }
    setGenerating(true);
    try {
      const r = await api.libraryGenerate(kind, genPrompt);
      setBody(r.draft || '');
      toast.success('Draft generated — review and save');
    } catch (e: any) {
      toast.error(`Generate failed: ${e.message}`);
    } finally { setGenerating(false); }
  }

  async function del(itemName: string) {
    if (!window.confirm(`Delete ${meta.one} "${itemName}"? This removes its file.`)) return;
    try {
      await api.libraryDelete(kind, itemName);
      toast.success(`Deleted ${meta.one} "${itemName}"`);
      qc.invalidateQueries({ queryKey: ['library', kind] });
    } catch (e: any) { toast.error(e.message); }
  }

  async function clearAll() {
    if (!window.confirm(`Clear ALL ${meta.title.toLowerCase()} (custom + default)? This cannot be undone.`)) return;
    try {
      const r = await api.libraryClear(kind);
      toast.success(`Cleared ${r.removed} ${meta.one}${r.removed === 1 ? '' : 's'}`);
      qc.invalidateQueries({ queryKey: ['library', kind] });
    } catch (e: any) { toast.error(e.message); }
  }

  async function save() {
    if (!name.trim() || !body.trim()) { toast.error('Name and body are required'); return; }
    try {
      await api.libraryCreate(kind, {
        name, description,
        triggers: triggers.split(',').map(t => t.trim()).filter(Boolean),
        body, always: kind === 'rules',
      });
      toast.success(`Saved ${meta.one} "${name}"`);
      reset(); setCreating(false);
      setTab('custom');   // new items are custom — show them where they land
      qc.invalidateQueries({ queryKey: ['library', kind] });
    } catch (e: any) { toast.error(e.message); }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>{meta.title}</h1>
          <div className="subtitle">{items.length} {meta.one}{items.length === 1 ? '' : 's'} · {meta.blurb}</div>
        </div>
        <div className="row">
          <button className="ghost" title={`Chat with an agent to build a ${meta.one}`}
                  onClick={() => navigate(`/chat?builder=${meta.one}`)}>
            <Icon.Chat size={14} /> New {meta.one} via chat
          </button>
          <button onClick={() => setCreating(c => !c)}>
            {creating ? <><Icon.X size={14} /> Cancel</> : <><Icon.Plus size={14} /> New {meta.one}</>}
          </button>
          {items.length > 0 && (
            <button className="ghost danger" title={`Delete every ${meta.one}`} onClick={clearAll}>
              <Icon.Trash size={14} /> Clear all
            </button>
          )}
        </div>
      </div>

      {creating && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-header"><h2>New {meta.one}</h2></div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '4px 2px' }}>
            {/* generate via LLM */}
            <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
              <label style={{ flex: 1 }}>
                <div className="muted small">Generate with the configured model (optional)</div>
                <input value={genPrompt} onChange={e => setGenPrompt(e.target.value)}
                  placeholder={`Describe the ${meta.one}, e.g. "how to add a Stripe webhook endpoint"`} />
              </label>
              <button className="ghost" onClick={generate} disabled={generating}>
                {generating ? 'Generating…' : <><Icon.Chat size={14} /> Generate draft</>}
              </button>
            </div>
            <hr style={{ border: 0, borderTop: '1px solid var(--border-1)', margin: '2px 0' }} />
            {/* manual fields */}
            <label>
              <div className="muted small">Name</div>
              <input value={name} onChange={e => setName(e.target.value)} placeholder={`${meta.one}-name`} />
            </label>
            {kind !== 'rules' && (
              <>
                <label>
                  <div className="muted small">Description (used for relevance matching)</div>
                  <input value={description} onChange={e => setDescription(e.target.value)} placeholder="one-line summary" />
                </label>
                <label>
                  <div className="muted small">Triggers (comma-separated keywords)</div>
                  <input value={triggers} onChange={e => setTriggers(e.target.value)} placeholder="stripe, webhook, payment" />
                </label>
              </>
            )}
            <label>
              <div className="muted small">Body (markdown — the instructions)</div>
              <textarea value={body} onChange={e => setBody(e.target.value)} rows={12}
                style={{ fontFamily: 'monospace', fontSize: 12 }}
                placeholder={kind === 'rules' ? '# Title\n- imperative bullet the agent must follow' : 'Step-by-step instructions the agent follows…'} />
            </label>
            <div className="row" style={{ justifyContent: 'flex-end', gap: 8 }}>
              <button className="ghost" onClick={() => { reset(); setCreating(false); }}>Cancel</button>
              <button onClick={save}><Icon.Plus size={14} /> Save {meta.one}</button>
            </div>
          </div>
        </div>
      )}

      {/* ── Default | Custom tabs ─────────────────────────────────── */}
      <div className="row" style={{ gap: 4, marginBottom: 16, borderBottom: '1px solid var(--border-1)' }}>
        {(['default', 'custom'] as const).map(t => (
          <button key={t} onClick={() => setTab(t)}
                  className={tab === t ? '' : 'ghost'}
                  style={{ borderRadius: '6px 6px 0 0', textTransform: 'capitalize',
                           fontWeight: tab === t ? 600 : 400 }}>
            {t}
          </button>
        ))}
      </div>

      {isLoading ? (
        <div className="skeleton" style={{ height: 120 }} />
      ) : shown.length === 0 ? (
        <div className="empty">
          {tab === 'default'
            ? `No default ${meta.one}s.`
            : `No custom ${meta.one}s yet — create one above.`}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {shown.map((it: any) => {
            const open = expanded === it.name;
            return (
              <div key={it.name + it.source} className="card" style={{ padding: '12px 16px' }}>
                <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start', cursor: 'pointer' }}
                  {...clickable(() => setExpanded(open ? null : it.name))}>
                  <div style={{ minWidth: 0 }}>
                    <strong>{it.name}</strong>
                    {it.always && <span className="chip sm" style={{ marginLeft: 6 }}>always-on</span>}
                    {it.description && <div className="muted small">{it.description}</div>}
                    {kind === 'rules' && it.globs?.length > 0 && (
                      <div className="muted small mono">globs: {it.globs.join(', ')}</div>
                    )}
                    {it.triggers?.length > 0 && (
                      <div style={{ marginTop: 4, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                        {it.triggers.slice(0, 8).map((t: string) => (
                          <span key={t} className="chip sm">{t}</span>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                    <button className="ghost danger" title={`Delete ${meta.one}`}
                      onClick={e => { e.stopPropagation(); del(it.name); }}
                      style={{ padding: '2px 8px' }}>
                      <Icon.Trash size={13} />
                    </button>
                    <span className="muted small">{open ? '▲' : '▼'}</span>
                  </div>
                </div>
                {open && (
                  <div className="bubble-body" style={{ marginTop: 10 }}>
                    <MdLite text={it.body || ''} />
                    {it.source && <div className="muted small mono" style={{ marginTop: 8 }}>{it.source}</div>}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
