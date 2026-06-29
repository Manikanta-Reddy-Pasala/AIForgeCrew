// Integration setup cards (Confluence / Jira / GitLab) + a panel that groups
// them. Extracted from Home so they can live in an Integrations section below
// the Chat composer — closest to where the chat tools they enable are used.
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { integrationsApi } from '../api';

const inputStyle = { width: '100%', maxWidth: 460, padding: '6px 8px', fontSize: 13 };

// ── Confluence integration card ──────────────────────────────────────────────
export function ConfluenceCard() {
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

// ── Jira integration card ────────────────────────────────────────────────────
export function JiraCard() {
  const [baseUrl, setBaseUrl] = useState('');
  const [token, setToken] = useState('');
  const [user, setUser] = useState('');
  const [authMode, setAuthMode] = useState<'pat' | 'basic'>('pat');
  const [insecure, setInsecure] = useState(false);
  const [hasToken, setHasToken] = useState(false);
  const [envManaged, setEnvManaged] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    integrationsApi.getJira().then(c => {
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
      const c = await integrationsApi.setJira({
        base_url: baseUrl.trim(),
        user: authMode === 'basic' ? user.trim() : '',
        insecure_tls: insecure,
        ...(token.trim() ? { token: token.trim() } : {}),
      });
      setHasToken(!!c.has_token);
      setToken('');
      toast.success('Jira settings saved');
    } catch (e: any) {
      toast.error(`Save failed: ${e.message}`);
    } finally { setBusy(false); }
  }

  async function test() {
    setBusy(true);
    try {
      const r = await integrationsApi.testJira();
      if (r.ok) toast.success(`Connected to ${r.base_url || 'Jira'} as ${r.user || '?'} (${r.auth} auth)`);
      else {
        const extra = r.denied_reason ? ` [${r.denied_reason}]` : (r.detail ? ` — ${r.detail}` : '');
        toast.error(`${r.error || 'Test failed'} (${r.auth} auth)${extra}${r.hint ? ` — ${r.hint}` : ''}`, { duration: 14000 });
      }
    } catch (e: any) {
      toast.error(`Test failed: ${e.message}`);
    } finally { setBusy(false); }
  }

  return (
    <div style={{ marginTop: 6 }}>
      <div className="small muted" style={{ marginBottom: 8 }}>
        <strong>Jira</strong> (Server / Data Center) — chat tools to search, read,
        create, update &amp; comment on issues. Writes go through the chat approval gate.
        {envManaged && <span style={{ color: 'var(--warn, #f59e0b)' }}> · currently set via env (overrides this form)</span>}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 460 }}>
        <label className="small">Base URL
          <input style={inputStyle} placeholder="https://jira.yourco.internal"
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
          Create a token in Jira: avatar → Profile → Personal Access Tokens → Create token.
        </div>
      </div>
    </div>
  );
}

// ── GitLab integration card ──────────────────────────────────────────────────
export function GitlabCard() {
  const [baseUrl, setBaseUrl] = useState('');
  const [token, setToken] = useState('');
  const [project, setProject] = useState('');
  const [oauth, setOauth] = useState(false);
  const [insecure, setInsecure] = useState(false);
  const [hasToken, setHasToken] = useState(false);
  const [envManaged, setEnvManaged] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    integrationsApi.getGitlab().then(c => {
      setBaseUrl(c.base_url || '');
      setProject(c.project || '');
      setOauth(!!c.oauth);
      setInsecure(!!c.insecure_tls);
      setHasToken(!!c.has_token);
      setEnvManaged(!!c.env_managed);
    }).catch(() => { /* endpoint may be absent on old API */ });
  }, []);

  async function save() {
    setBusy(true);
    try {
      const c = await integrationsApi.setGitlab({
        base_url: baseUrl.trim(),
        project: project.trim(),
        oauth,
        insecure_tls: insecure,
        ...(token.trim() ? { token: token.trim() } : {}),
      });
      setHasToken(!!c.has_token);
      setToken('');
      toast.success('GitLab settings saved');
    } catch (e: any) {
      toast.error(`Save failed: ${e.message}`);
    } finally { setBusy(false); }
  }

  async function test() {
    setBusy(true);
    try {
      const r = await integrationsApi.testGitlab();
      if (r.ok) toast.success(`Connected to ${r.base_url || 'GitLab'} as ${r.user || '?'} (${r.auth} auth)`);
      else {
        const extra = r.detail ? ` — ${r.detail}` : '';
        toast.error(`${r.error || 'Test failed'} (${r.auth} auth)${extra}${r.hint ? ` — ${r.hint}` : ''}`, { duration: 14000 });
      }
    } catch (e: any) {
      toast.error(`Test failed: ${e.message}`);
    } finally { setBusy(false); }
  }

  return (
    <div style={{ marginTop: 6 }}>
      <div className="small muted" style={{ marginBottom: 8 }}>
        <strong>GitLab</strong> (self-managed / SaaS) — chat tools to search, read,
        create, update &amp; comment on issues. Writes go through the chat approval gate.
        {envManaged && <span style={{ color: 'var(--warn, #f59e0b)' }}> · currently set via env (overrides this form)</span>}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 460 }}>
        <label className="small">Base URL
          <input style={inputStyle} placeholder="https://gitlab.yourco.internal"
                 value={baseUrl} onChange={e => setBaseUrl(e.target.value)} />
        </label>
        <label className="small">Default project <span className="muted">(optional)</span>
          <input style={inputStyle} placeholder="group/subgroup/project  (or numeric id)"
                 value={project} onChange={e => setProject(e.target.value)} />
        </label>
        <label className="small">Access Token
          <input style={inputStyle} type="password"
                 placeholder={hasToken ? '•••••• (leave blank to keep)' : 'paste PAT / project / group token'}
                 value={token} onChange={e => setToken(e.target.value)} />
        </label>
        <label className="row small" style={{ gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={oauth} onChange={e => setOauth(e.target.checked)} />
          Token is OAuth (send as Bearer instead of PRIVATE-TOKEN)
        </label>
        <label className="row small" style={{ gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={insecure} onChange={e => setInsecure(e.target.checked)} />
          Skip TLS verify (self-signed internal cert)
        </label>
        <div className="row" style={{ gap: 8 }}>
          <button onClick={save} disabled={busy || !baseUrl.trim()}>Save</button>
          <button className="ghost" onClick={test} disabled={busy}>Test connection</button>
        </div>
        <div className="xs muted">
          Create a token in GitLab: avatar → Edit profile → Access Tokens (needs <code>read_api</code>, plus <code>api</code> for writes).
        </div>
      </div>
    </div>
  );
}

// ── Grouped panel: all three integration cards, divider-separated ─────────────
// Collapsible so it sits unobtrusively in a section below the Chat composer.
export function IntegrationsPanel() {
  const [open, setOpen] = useState(false);
  const divider = <div style={{ height: 1, background: 'var(--border, #2a2a2a)', margin: '16px 0' }} />;
  return (
    <div className="card" style={{ marginTop: 12 }}>
      <button
        className="ghost"
        onClick={() => setOpen(o => !o)}
        style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%',
                 justifyContent: 'space-between', background: 'none', border: 'none',
                 padding: 0, cursor: 'pointer' }}
        title="Connect Jira / Confluence / GitLab so the chat agent can use them"
      >
        <h2 style={{ fontSize: 14, margin: 0 }}>Integrations</h2>
        <span className="small muted">{open ? '▾ hide' : '▸ Jira · Confluence · GitLab'}</span>
      </button>
      {open && (
        <div style={{ marginTop: 8, maxHeight: '42vh', overflow: 'auto' }}>
          <ConfluenceCard />
          {divider}
          <JiraCard />
          {divider}
          <GitlabCard />
        </div>
      )}
    </div>
  );
}
