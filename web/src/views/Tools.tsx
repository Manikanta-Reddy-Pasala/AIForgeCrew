// MCP tools view.
//
// The operator reset MCP to a clean slate (2026-06-26) — zero servers ship
// by default. Endpoints are configured out-of-band via the
// AIFORGE_MCP_ENDPOINTS env var; until any are present this page shows a
// calm empty state instead of dead tool buttons that 503 against a missing
// binary / unreachable host.
import { Icon } from '../icons';

export default function Tools() {
  return (
    <>
      <div className="page-header">
        <div>
          <h1>MCP tools</h1>
          <div className="subtitle">Direct MCP tool surface for the Planner + Doer agents.</div>
        </div>
      </div>

      <div className="card" style={{ textAlign: 'center', padding: '48px 24px' }}>
        <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 12, opacity: 0.5 }}>
          <Icon.Sparkles size={28} />
        </div>
        <h2 style={{ margin: '0 0 8px' }}>No MCP servers configured</h2>
        <div className="subtitle" style={{ maxWidth: 460, margin: '0 auto' }}>
          MCP is disabled by default. Add servers by setting the{' '}
          <code style={{ fontSize: 12 }}>AIFORGE_MCP_ENDPOINTS</code> environment
          variable (CSV of <code style={{ fontSize: 12 }}>name=url</code> pairs),
          then reload this page.
        </div>
      </div>
    </>
  );
}
