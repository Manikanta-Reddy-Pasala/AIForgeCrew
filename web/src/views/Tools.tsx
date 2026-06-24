import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import { JsonView, darkStyles } from 'react-json-view-lite';
import 'react-json-view-lite/dist/index.css';
import { api } from '../api';
import { Icon } from '../icons';

type ArgSpec = { name: string; placeholder?: string; required?: boolean; numeric?: boolean };
type Tool = { name: string; desc: string; category: string; args: ArgSpec[] };

const CATEGORIES = ['Graph', 'Code', 'Kubernetes', 'Memory', 'Tickets', 'Docs'] as const;

const TOOLS: Tool[] = [
  // Graph / symbol
  { name: 'sym_lookup', category: 'Code', desc: 'Find declarations + call sites of a symbol across indexed repos.',
    args: [
      { name: 'name', placeholder: 'e.g. MessageRetryService', required: true },
      { name: 'repo', placeholder: 'optional repo filter' },
    ] },
  { name: 'list_repos', category: 'Code', desc: 'List every repo indexed in Neo4j.', args: [] },
  { name: 'list_services', category: 'Code', desc: 'List Spring/Flask services + their endpoints.', args: [] },
  { name: 'list_endpoints', category: 'Code', desc: 'Enumerate HTTP endpoints.',
    args: [{ name: 'repo', placeholder: 'optional repo filter' }] },
  { name: 'read_source', category: 'Code', desc: 'Read a source file (with optional line range).',
    args: [
      { name: 'path', placeholder: 'repo/src/.../File.java', required: true },
      { name: 'start_line', placeholder: '1', numeric: true },
      { name: 'end_line', placeholder: '80', numeric: true },
    ] },

  // Graph walk
  { name: 'graph_neighborhood', category: 'Graph', desc: 'Graph walk around a symbol.',
    args: [
      { name: 'symbol', placeholder: 'qualified symbol', required: true },
      { name: 'depth', placeholder: '1-3', numeric: true },
    ] },
  { name: 'caller_chain', category: 'Graph', desc: 'Who calls this symbol (transitive).',
    args: [{ name: 'symbol', placeholder: 'qualified symbol', required: true }] },
  { name: 'callee_chain', category: 'Graph', desc: 'What this symbol calls.',
    args: [{ name: 'symbol', placeholder: 'qualified symbol', required: true }] },
  { name: 'impact', category: 'Graph', desc: 'Blast-radius of changing a symbol.',
    args: [{ name: 'symbol', placeholder: 'qualified symbol', required: true }] },
  { name: 'cross_repo_flow', category: 'Graph', desc: 'Trace a request flow across repos.',
    args: [{ name: 'entry_symbol', placeholder: 'Controller method' }] },

  // Memory
  { name: 'related_memories', category: 'Memory', desc: 'Pull memory hits related to a free-text query.',
    args: [
      { name: 'query', placeholder: 'e.g. pagination pos backend', required: true },
      { name: 'top_k', placeholder: '6', numeric: true },
    ] },

  // Tickets
  { name: 'ticket_brief', category: 'Tickets', desc: 'Concise brief of a ticket (title, body, last status).',
    args: [{ name: 'identifier', placeholder: 'ONE-46', required: true }] },
  { name: 'ticket_fetch', category: 'Tickets', desc: 'Full ticket payload by identifier.',
    args: [{ name: 'identifier', placeholder: 'ONE-46', required: true }] },

  // Kubernetes
  { name: 'kube_status', category: 'Kubernetes', desc: 'Summary of deployed pods.', args: [] },

  // Docs
  { name: 'find_doc', category: 'Docs', desc: 'Full-text search across docs (any *.md).',
    args: [{ name: 'query', placeholder: 'search text', required: true }] },

  // Confluence (chat) — writes go through the approval gate
  { name: 'confluence_search', category: 'Confluence', desc: 'Find pages (full-text or CQL).',
    args: [{ name: 'query', placeholder: 'text, or use cql' }] },
  { name: 'confluence_read', category: 'Confluence', desc: 'Read a page (by id, or title + space).',
    args: [{ name: 'id', placeholder: 'page id' }] },
  { name: 'confluence_create', category: 'Confluence', desc: 'Create a page (needs Approve).',
    args: [{ name: 'title', placeholder: 'page title', required: true }] },
  { name: 'confluence_update', category: 'Confluence', desc: 'Update a page body (needs Approve).',
    args: [{ name: 'id', placeholder: 'page id', required: true }] },

  // Jira (chat) — writes go through the approval gate
  { name: 'jira_search', category: 'Jira', desc: 'Find issues (full-text or JQL).',
    args: [{ name: 'query', placeholder: 'text, or use jql' }] },
  { name: 'jira_read', category: 'Jira', desc: 'Read an issue (fields + comments).',
    args: [{ name: 'key', placeholder: 'ENG-123', required: true }] },
  { name: 'jira_create', category: 'Jira', desc: 'Create an issue (needs Approve).',
    args: [{ name: 'project', placeholder: 'project key', required: true }] },
  { name: 'jira_update', category: 'Jira', desc: 'Update issue fields (needs Approve).',
    args: [{ name: 'key', placeholder: 'ENG-123', required: true }] },
  { name: 'jira_comment', category: 'Jira', desc: 'Add a comment to an issue (needs Approve).',
    args: [{ name: 'key', placeholder: 'ENG-123', required: true }] },
];

export default function Tools() {
  const [active, setActive] = useState<string>(TOOLS[0].name);
  const [args, setArgs] = useState<Record<string, string>>({});
  const [result, setResult] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [railSearch, setRailSearch] = useState('');

  const tool = TOOLS.find(t => t.name === active) || TOOLS[0];

  const railGrouped = useMemo(() => {
    const s = railSearch.trim().toLowerCase();
    const by: Record<string, Tool[]> = {};
    for (const t of TOOLS) {
      if (s && !`${t.name} ${t.desc}`.toLowerCase().includes(s)) continue;
      (by[t.category] ??= []).push(t);
    }
    return by;
  }, [railSearch]);

  async function run() {
    setBusy(true); setErr(null); setResult(null);
    try {
      // validate required
      for (const a of tool.args) {
        if (a.required && !(args[a.name] || '').trim()) {
          throw new Error(`Missing required: ${a.name}`);
        }
      }
      const payload: Record<string, any> = {};
      for (const a of tool.args) {
        const v = (args[a.name] || '').trim();
        if (!v) continue;
        if (a.numeric && /^-?\d+$/.test(v)) payload[a.name] = parseInt(v, 10);
        else payload[a.name] = v;
      }
      const r = await api.mcpTool(tool.name, payload);
      setResult(r.result);
      toast.success(`${tool.name} ran`);
    } catch (e: any) {
      setErr(e.message || String(e));
      toast.error(`${tool.name}: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }

  function selectTool(name: string) {
    setActive(name);
    setArgs({});
    setResult(null);
    setErr(null);
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>MCP tools</h1>
          <div className="subtitle">Invoke `graph_rag` and helper MCPs directly — the same surface the Planner + Doer agents use.</div>
        </div>
      </div>

      <div className="tools-shell">
        <aside className="tools-rail">
          <div className="input-search" style={{ margin: 4, marginBottom: 8 }}>
            <Icon.Search size={14} />
            <input placeholder="filter tools…" value={railSearch} onChange={e => setRailSearch(e.target.value)} />
          </div>
          {CATEGORIES.map(cat => {
            const items = railGrouped[cat] || [];
            if (items.length === 0) return null;
            return (
              <div key={cat}>
                <div className="tool-group-label">{cat}</div>
                {items.map(t => (
                  <div
                    key={t.name}
                    className={`tool-item ${active === t.name ? 'on' : ''}`}
                    onClick={() => selectTool(t.name)}
                  >
                    <span className="tool-name">{t.name}</span>
                    <span className="tool-desc">{t.desc}</span>
                  </div>
                ))}
              </div>
            );
          })}
        </aside>

        <section className="stack">
          <div className="card">
            <div className="card-header">
              <div>
                <div className="row" style={{ gap: 8 }}>
                  <span className="chip">{tool.category}</span>
                  <h2 className="mono" style={{ fontSize: 16 }}>{tool.name}</h2>
                </div>
                <div className="subtitle" style={{ marginTop: 4 }}>{tool.desc}</div>
              </div>
              <button onClick={run} disabled={busy}>
                {busy ? 'Running…' : <><Icon.Sparkles size={14} /> Invoke</>}
              </button>
            </div>

            {tool.args.length === 0 ? (
              <div className="muted small">This tool takes no arguments.</div>
            ) : (
              <div className="stack">
                {tool.args.map(a => (
                  <label className="field" key={a.name}>
                    <span>
                      {a.name}
                      {a.required && <span style={{ color: 'var(--err)', marginLeft: 4 }}>*</span>}
                      {a.numeric && <span className="muted" style={{ marginLeft: 6 }}>int</span>}
                    </span>
                    <input
                      placeholder={a.placeholder}
                      value={args[a.name] || ''}
                      onChange={e => setArgs(s => ({ ...s, [a.name]: e.target.value }))}
                      onKeyDown={e => { if (e.key === 'Enter' && !busy) run(); }}
                    />
                  </label>
                ))}
              </div>
            )}
          </div>

          {err && (
            <div className="card" style={{ borderColor: 'var(--err)' }}>
              <div className="row"><span className="chip err">Error</span><span className="small">{err}</span></div>
            </div>
          )}

          {result && (
            <div className="card">
              <div className="card-header">
                <h2>Result</h2>
                <button className="ghost sm" onClick={() => {
                  navigator.clipboard.writeText(JSON.stringify(result, null, 2));
                  toast.success('Result copied to clipboard');
                }}>Copy JSON</button>
              </div>
              <div className="tool-result">
                <JsonView
                  data={result}
                  shouldExpandNode={(level) => level < 2}
                  style={{
                    ...darkStyles,
                    container: 'json-view',
                  }}
                />
              </div>
            </div>
          )}
        </section>
      </div>
    </>
  );
}
