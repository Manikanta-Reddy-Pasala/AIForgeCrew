// ── OKR-DAG: the goal-oriented memory (objectives → key results → learnings)
// OKF (Open Knowledge Format) meta for one concept node: a type badge, the
// one-line description, tag chips, and link count (linked_krs = graph edges).
export function OkfMeta({ n }: Readonly<{ n: any }>) {
  const tags: string[] = Array.isArray(n.tags) ? n.tags : [];
  const links: string[] = Array.isArray(n.linked_krs) ? n.linked_krs : [];
  return (
    <>
      {n.description && <div className="muted xs" style={{ marginTop: 2 }}>{n.description}</div>}
      {(tags.length > 0 || links.length > 0) && (
        <div className="row" style={{ gap: 4, marginTop: 3, flexWrap: 'wrap' }}>
          {tags.map(t => <span key={t} className="chip xs" title="tag">#{t}</span>)}
          {links.length > 0 && <span className="muted xs" title="linked concepts (OKF edges)">🔗 {links.length}</span>}
        </div>
      )}
    </>
  );
}
