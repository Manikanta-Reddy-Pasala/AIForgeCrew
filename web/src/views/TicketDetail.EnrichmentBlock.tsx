import { Link } from 'react-router-dom';

export function EnrichmentBlock({ enrichment }: { enrichment: any }) {
  const i = enrichment.intent || {};
  const focal = enrichment.focal_files || [];
  const ref = enrichment.reference_files || [];
  const sims = enrichment.similar_tickets || [];
  const t3 = enrichment.t3_recipes || [];
  const cmds = enrichment.commands || {};
  const sources = enrichment.sources_used || [];
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
        <span className="chip sm" style={{ fontWeight: 600 }}>{i.action || '?'}</span>
        <span className="chip sm">entity: <code>{i.entity || '?'}</code></span>
        {i.reference_pattern && (
          <span className="chip sm">ref: <code>{i.reference_pattern}</code></span>
        )}
        {enrichment.repo && (
          <span className="chip sm">repo: <code>{enrichment.repo}</code></span>
        )}
      </div>
      {i.keywords?.length > 0 && (
        <div className="small muted">
          keywords: {i.keywords.join(', ')}
        </div>
      )}
      {focal.length > 0 && (
        <details>
          <summary className="small"><strong>Focal files</strong> ({focal.length})</summary>
          <ul className="small mono" style={{ marginTop: 4 }}>
            {focal.map((f: string, k: number) => <li key={k}>{f}</li>)}
          </ul>
        </details>
      )}
      {ref.length > 0 && (
        <details>
          <summary className="small"><strong>Reference files</strong> ({ref.length})</summary>
          <ul className="small mono" style={{ marginTop: 4 }}>
            {ref.map((f: string, k: number) => <li key={k}>{f}</li>)}
          </ul>
        </details>
      )}
      {sims.length > 0 && (
        <details>
          <summary className="small"><strong>Similar past tickets</strong> ({sims.length})</summary>
          <ul className="small" style={{ marginTop: 4 }}>
            {sims.map((s: any, k: number) => (
              <li key={k}>
                <Link to={`/tickets/${s.identifier}`} className="identifier-badge">
                  {s.identifier}
                </Link>
                {' '}<span className="muted">({s.status})</span> — {s.title?.slice(0, 90)}
              </li>
            ))}
          </ul>
        </details>
      )}
      {t3.length > 0 && (
        <details>
          <summary className="small"><strong>T3 recipes</strong> ({t3.length})</summary>
          <ul className="small" style={{ marginTop: 4 }}>
            {t3.map((r: string, k: number) => <li key={k}>{r.slice(0, 280)}</li>)}
          </ul>
        </details>
      )}
      {Object.keys(cmds).length > 0 && (
        <details>
          <summary className="small"><strong>Build commands</strong></summary>
          <ul className="small mono" style={{ marginTop: 4 }}>
            {Object.entries(cmds).map(([k, v]) => (
              <li key={k}><strong>{k}:</strong> {String(v)}</li>
            ))}
          </ul>
        </details>
      )}
      <div className="small muted">sources: {sources.join(', ')}</div>
    </div>
  );
}
