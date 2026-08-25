import { truncate } from './Memory.helpers';

export function HitCard({ h }: Readonly<{ h: any }>) {
  return (
    <div className="card" style={{ padding: '10px 12px' }}>
      <div className="row small muted" style={{ gap: 8, marginBottom: 4 }}>
        <span className="mem-wing-pill">{h.wing || 'memory'}</span>
        {h.linked && <span className="mem-wing-pill">linked</span>}
        {h.source && <span>{truncate(h.source, 32)}</span>}
        {h.metadata?.repo && <span>· {h.metadata.repo}</span>}
        {typeof h.score === 'number' && (
          <span style={{ marginLeft: 'auto' }}>score {h.score.toFixed(2)}</span>
        )}
      </div>
      <div style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>{h.text}</div>
    </div>
  );
}
