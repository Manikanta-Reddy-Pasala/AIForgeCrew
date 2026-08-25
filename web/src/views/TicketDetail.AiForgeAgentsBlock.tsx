export function AiForgeAgentsBlock({ m }: Readonly<{ m: any }>) {
  const plan = m.plan || {};
  const steps = plan.steps || [];
  const grounding = m.grounding || {};
  const verifier = m.verifier || {};
  const doer = m.doer || {};
  const validation = m.validation || {};
  const review = m.review || {};
  const learning = m.learning || {};
  const stages = m.stages_s || {};
  const allowed = m.allowed_files || [];
  const allowedCount = m.allowed_files_count || allowed.length;

  return (
    <div className="card">
      <div className="card-header">
        <h2>AIForge Agents pipeline</h2>
      </div>
      <div className="stack" style={{ gap: 12 }}>
        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          <span className="chip sm">verdict: <code>{verifier.verdict || '?'}</code></span>
          <span className="chip sm">grounded: <code>{String(grounding.resolved ?? '?')}</code></span>
          <span className="chip sm">validation: <code>{validation.decision || '?'}</code></span>
          <span className="chip sm">review: <code>{review.decision || '?'}</code></span>
          <span className="chip sm">outcome: <code>{learning.outcome || '?'}</code></span>
          <span className="chip sm">latency: <code>{m.latency_s || '?'}s</code></span>
        </div>

        {Object.keys(stages).length > 0 && (
          <details>
            <summary className="small"><strong>Stage timings (s)</strong></summary>
            <ul className="small mono" style={{ marginTop: 4 }}>
              {Object.entries(stages).map(([k, v]) => (
                <li key={k}>{k}: {String(v)}</li>
              ))}
            </ul>
          </details>
        )}

        {steps.length > 0 && (
          <details open>
            <summary className="small"><strong>Plan steps</strong> ({steps.length})</summary>
            <ol className="small mono" style={{ marginTop: 4 }}>
              {steps.map((s: any, k: number) => (
                <li key={k}>
                  <span className="chip sm" style={{ marginRight: 6 }}>{s.action}</span>
                  <code>{s.target}</code>
                  {s.expected && <div className="muted">↳ {s.expected}</div>}
                </li>
              ))}
            </ol>
          </details>
        )}

        {grounding.unresolved_refs?.length > 0 && (
          <details>
            <summary className="small"><strong>Unresolved refs</strong> ({grounding.unresolved_refs.length})</summary>
            <ul className="small mono" style={{ marginTop: 4 }}>
              {grounding.unresolved_refs.map((u: any, k: number) => (
                <li key={k}><code>{u.target}</code> — {u.reason} ({u.action})</li>
              ))}
            </ul>
          </details>
        )}

        {verifier.issues?.length > 0 && (
          <details>
            <summary className="small"><strong>Verifier issues</strong> ({verifier.issues.length})</summary>
            <ul className="small" style={{ marginTop: 4 }}>
              {verifier.issues.map((i: any, k: number) => (
                <li key={k}>
                  <strong>{i.kind}</strong> {i.step_id ? `(step ${i.step_id})` : ''}: {i.message}
                </li>
              ))}
            </ul>
          </details>
        )}

        {doer.problems?.length > 0 && (
          <details>
            <summary className="small"><strong>Doer detector hits</strong> ({doer.problems.length})</summary>
            <ul className="small mono" style={{ marginTop: 4 }}>
              {doer.problems.map((p: any, k: number) => (
                <li key={k}><strong>{p.mode}</strong> — {p.evidence}</li>
              ))}
            </ul>
          </details>
        )}

        {doer.udiff && (
          <details>
            <summary className="small"><strong>Doer udiff</strong> {doer.target ? `→ ${doer.target}` : ''}</summary>
            <pre className="small mono" style={{
              marginTop: 4, maxHeight: 360, overflow: 'auto',
              background: 'var(--bg-2)', padding: 8, borderRadius: 4,
            }}>{doer.udiff}</pre>
            {doer.artifact_path && (
              <div className="small muted">full patch: <code>{doer.artifact_path}</code></div>
            )}
            <div className="small muted">
              applied: <code>{String(doer.applied || false)}</code>
              {doer.applied_branch && <> · branch: <code>{doer.applied_branch}</code></>}
              {doer.apply_error && <> · err: <span style={{ color: 'tomato' }}>{doer.apply_error}</span></>}
            </div>
          </details>
        )}

        {(review.mr_title || review.mr_body) && (
          <details open>
            <summary className="small"><strong>Architect MR</strong> — {review.mr_title || '(untitled)'}</summary>
            {review.mr_url && (
              <div className="small">
                <a href={review.mr_url} target="_blank" rel="noopener">
                  {review.mr_url}
                </a>
              </div>
            )}
            {review.mr_body && (
              <pre className="small" style={{
                whiteSpace: 'pre-wrap', marginTop: 4,
                background: 'var(--bg-2)', padding: 8, borderRadius: 4,
              }}>{review.mr_body}</pre>
            )}
            {review.comments?.length > 0 && (
              <ul className="small" style={{ marginTop: 4 }}>
                {review.comments.map((c: string, k: number) => (
                  <li key={k}>{c}</li>
                ))}
              </ul>
            )}
          </details>
        )}

        {allowed.length > 0 && (
          <details>
            <summary className="small">
              <strong>Allowed files seeded</strong> (showing {allowed.length} of {allowedCount})
            </summary>
            <ul className="small mono" style={{ marginTop: 4 }}>
              {allowed.map((f: string, k: number) => <li key={k}>{f}</li>)}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}
