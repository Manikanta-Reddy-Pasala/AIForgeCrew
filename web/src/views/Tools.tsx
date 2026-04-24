import { useState } from 'react';
import { api } from '../api';

// Argument specs per tool. Keep tight — operator doesn't need to
// guess which fields each tool takes. Extend as we add tools.
type ArgSpec = { name: string; placeholder?: string; required?: boolean };
const TOOLS: Record<string, { desc: string; args: ArgSpec[] }> = {
  sym_lookup: {
    desc: 'Find declarations + call sites of a symbol across indexed repos.',
    args: [
      { name: 'name', placeholder: 'e.g. MessageRetryService', required: true },
      { name: 'repo', placeholder: 'optional repo filter' },
    ],
  },
  list_repos: { desc: 'List every repo indexed in Neo4j.', args: [] },
  list_services: { desc: 'List Spring/Flask services + their endpoints.', args: [] },
  list_endpoints: {
    desc: 'Enumerate HTTP endpoints.',
    args: [{ name: 'repo', placeholder: 'optional repo filter' }],
  },
  graph_neighborhood: {
    desc: 'Graph walk around a symbol.',
    args: [
      { name: 'symbol', placeholder: 'qualified symbol', required: true },
      { name: 'depth', placeholder: '1-3' },
    ],
  },
  caller_chain: {
    desc: 'Who calls this symbol (transitive).',
    args: [{ name: 'symbol', placeholder: 'qualified symbol', required: true }],
  },
  callee_chain: {
    desc: 'What this symbol calls.',
    args: [{ name: 'symbol', placeholder: 'qualified symbol', required: true }],
  },
  read_source: {
    desc: 'Read a source file (with optional line range).',
    args: [
      { name: 'path', placeholder: 'repo/src/.../File.java', required: true },
      { name: 'start_line', placeholder: '1' },
      { name: 'end_line', placeholder: '80' },
    ],
  },
  impact: {
    desc: 'Blast-radius of changing a symbol.',
    args: [{ name: 'symbol', placeholder: 'qualified symbol', required: true }],
  },
  cross_repo_flow: {
    desc: 'Trace a request flow across repos.',
    args: [{ name: 'entry_symbol', placeholder: 'Controller method' }],
  },
  related_memories: {
    desc: 'Pull memory hits related to a free-text query.',
    args: [
      { name: 'query', placeholder: 'e.g. pagination pos backend', required: true },
      { name: 'top_k', placeholder: '6' },
    ],
  },
  ticket_brief: {
    desc: 'Concise brief of a ticket (title, body, last status).',
    args: [{ name: 'identifier', placeholder: 'ONE-46', required: true }],
  },
  ticket_fetch: {
    desc: 'Full ticket payload by identifier.',
    args: [{ name: 'identifier', placeholder: 'ONE-46', required: true }],
  },
  kube_status: { desc: 'Summary of deployed pods.', args: [] },
  find_doc: {
    desc: 'Full-text search across docs + CLAUDE.md.',
    args: [{ name: 'query', placeholder: 'search text', required: true }],
  },
};

export default function Tools() {
  const names = Object.keys(TOOLS);
  const [tool, setTool] = useState(names[0]);
  const [args, setArgs] = useState<Record<string, string>>({});
  const [result, setResult] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      // Coerce numeric strings; drop empty.
      const payload: Record<string, any> = {};
      for (const k of Object.keys(args)) {
        const v = args[k];
        if (v === undefined || v === '') continue;
        if (/^-?\d+$/.test(v)) payload[k] = parseInt(v, 10);
        else payload[k] = v;
      }
      const r = await api.mcpTool(tool, payload);
      setResult(r.result);
    } catch (e: any) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  const spec = TOOLS[tool];
  return (
    <>
      <h1>MCP Tools</h1>
      <p className="muted small">
        Invoke graph_rag MCP tools directly against Neo4j. Same tools
        the Planner + Doer agents use mid-run.
      </p>

      <div className="card">
        <div className="row" style={{ gap: '.5rem', marginBottom: '.5rem' }}>
          <label>Tool:{' '}
            <select value={tool} onChange={e => {
              setTool(e.target.value);
              setArgs({});
              setResult(null);
              setErr(null);
            }}>
              {names.map(n => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <span className="small muted">{spec.desc}</span>
        </div>
        {spec.args.map(a => (
          <div key={a.name} style={{ marginBottom: '.4rem' }}>
            <input
              placeholder={`${a.name}${a.required ? ' *' : ''}: ${a.placeholder || ''}`}
              value={args[a.name] || ''}
              onChange={e => setArgs(s => ({ ...s, [a.name]: e.target.value }))}
              style={{ width: '100%' }}
            />
          </div>
        ))}
        <button onClick={run} disabled={busy}>{busy ? 'Running…' : 'Run'}</button>
      </div>

      {err && <div className="card" style={{ color: '#f56565' }}>Error: {err}</div>}
      {result && (
        <div className="card">
          <h2>Result</h2>
          <pre className="small" style={{
            background: '#1a1a1a', padding: '.5rem',
            overflow: 'auto', maxHeight: '60vh',
          }}>{JSON.stringify(result, null, 2)}</pre>
        </div>
      )}
    </>
  );
}
