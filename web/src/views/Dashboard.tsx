import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ResponsiveContainer, AreaChart, Area, Tooltip } from 'recharts';
import { api } from '../api';
import { statusClass, priorityClass, relTime } from '../util';

export default function Dashboard() {
  const health  = useQuery({ queryKey: ['health'],  queryFn: () => api.health(), refetchInterval: 15_000 });
  const agents  = useQuery({ queryKey: ['agents'],  queryFn: () => api.agents() });
  const tickets = useQuery({ queryKey: ['tickets', 'dashboard'], queryFn: () => api.tickets({ limit: '25' }) });
  const mem     = useQuery({ queryKey: ['memory', 'stats'], queryFn: () => api.memoryStats() });

  const rows = tickets.data || [];
  const mStats = (mem.data as any) || { wings: [] };

  const counts = useMemo(() => {
    const c = { todo: 0, in_progress: 0, in_review: 0, done: 0, blocked: 0, total: rows.length };
    for (const r of rows) { (c as any)[r.status] = ((c as any)[r.status] || 0) + 1; }
    return c;
  }, [rows]);

  const totalMem    = (mStats.wings || []).reduce((a: number, w: any) => a + Number(w.n || 0), 0);
  const embeddedMem = (mStats.wings || []).reduce((a: number, w: any) => a + Number(w.embedded || 0), 0);

  // Build a trivial sparkline from status distribution so the card has a pulse.
  const spark = useMemo(() => {
    const seeds = [counts.done, counts.in_review, counts.in_progress, counts.blocked, counts.todo];
    return seeds.map((v, i) => ({ x: i, v: Math.max(1, v) }));
  }, [counts]);

  const inProgress = rows.filter((r: any) => r.status === 'in_progress');
  const blocked    = rows.filter((r: any) => r.status === 'blocked');

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Agent ops overview</h1>
          <div className="subtitle">
            Live picture of the AIForge crew — tickets in flight, memory depth, model reachability.
          </div>
        </div>
        <div className="row">
          <Link to="/board" className="chip info">Open board →</Link>
        </div>
      </div>

      <div className="grid grid-4" style={{ marginBottom: 20 }}>
        <Metric
          label="Tickets open"
          value={String(rows.length - counts.done)}
          sub={`${counts.in_progress} active · ${counts.blocked} blocked`}
          tone={counts.blocked > 0 ? 'warn' : ''}
          spark={spark}
        />
        <Metric
          label="Agents online"
          value={String(agents.data?.length || 0)}
          sub={`${(agents.data || []).reduce((a: number, x: any) => a + (x.active_tickets?.length || 0), 0)} assignments`}
        />
        <Metric
          label="Memory chunks"
          value={totalMem.toLocaleString()}
          sub={`${embeddedMem.toLocaleString()} embedded · ${(mStats.wings || []).length} wings`}
        />
        <Metric
          label="Runtime"
          value={health.data?.postgres && health.data?.lm_studio ? 'Healthy' : 'Degraded'}
          sub={`pg ${health.data?.postgres ? 'ok' : 'down'} · lm ${health.data?.lm_studio ? 'ok' : 'down'}`}
          tone={health.data?.postgres && health.data?.lm_studio ? 'ok' : 'err'}
        />
      </div>

      <div className="grid grid-2">
        <div className="card">
          <div className="card-header">
            <h2>In progress</h2>
            <Link to="/tickets" className="chip">{inProgress.length + blocked.length} live</Link>
          </div>
          {(inProgress.length + blocked.length) === 0 ? (
            <Empty title="Quiet pipeline" hint="No tickets are currently moving." />
          ) : (
            <table>
              <thead>
                <tr><th>ID</th><th>Status</th><th>Priority</th><th>Assignee</th><th>Title</th><th>Updated</th></tr>
              </thead>
              <tbody>
                {[...inProgress, ...blocked].slice(0, 10).map((t: any) => (
                  <tr key={t.id}>
                    <td><Link to={`/tickets/${t.identifier}`} className="identifier-badge">{t.identifier}</Link></td>
                    <td><span className={`chip ${statusClass(t.status)}`}>{t.status.replace('_', ' ')}</span></td>
                    <td><span className={`chip ${priorityClass(t.priority)}`}>{t.priority}</span></td>
                    <td className="muted small">{t.assignee_role || '—'}</td>
                    <td style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.title}</td>
                    <td className="muted small nowrap">{relTime(t.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <div className="card-header">
            <h2>Recent activity</h2>
            <Link to="/tickets" className="chip">all tickets →</Link>
          </div>
          {rows.length === 0 ? (
            <Empty title="No tickets yet" hint="Create one to get started." />
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column' }}>
              {rows.slice(0, 8).map((t: any) => (
                <Link
                  key={t.id}
                  to={`/tickets/${t.identifier}`}
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'auto auto 1fr auto',
                    gap: 10,
                    alignItems: 'center',
                    padding: '10px 4px',
                    borderBottom: '1px solid var(--border-0)',
                    color: 'inherit',
                  }}
                >
                  <span className="identifier-badge">{t.identifier}</span>
                  <span className={`chip ${statusClass(t.status)}`}>{t.status.replace('_', ' ')}</span>
                  <span style={{ color: 'var(--fg-0)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {t.title}
                  </span>
                  <span className="muted xs nowrap">{relTime(t.updated_at)}</span>
                </Link>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <div className="card">
          <div className="card-header">
            <h2>Agents</h2>
            <Link to="/agents" className="chip">manage →</Link>
          </div>
          {(agents.data || []).length === 0 ? (
            <Empty title="No agents registered" hint="Check the backend config." />
          ) : (
            <div>
              {(agents.data || []).map((a: any) => (
                <div key={a.role} className="agent-row">
                  <div className="agent-name">{a.role}</div>
                  <span className="chip mono">{a.model}</span>
                  <span className="chip">{a.transport}</span>
                  <span className="spacer" />
                  <span className="muted xs">
                    {a.active_tickets?.length || 0} active · {relTime(a.last_activity)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-header">
            <h2>Memory wings</h2>
            <Link to="/memory" className="chip">search →</Link>
          </div>
          {(mStats.wings || []).length === 0 ? (
            <Empty title="No memory data" hint="Memory stats endpoint returned empty." />
          ) : (
            <table>
              <thead><tr><th>Tier</th><th>Wing</th><th style={{ textAlign: 'right' }}>Count</th><th style={{ textAlign: 'right' }}>Embedded</th></tr></thead>
              <tbody>
                {(mStats.wings || []).slice(0, 10).map((w: any, i: number) => (
                  <tr key={i}>
                    <td><span className="chip">{w.tier}</span></td>
                    <td className="small mono">{w.wing}</td>
                    <td className="small mono" style={{ textAlign: 'right' }}>{Number(w.n).toLocaleString()}</td>
                    <td className="small mono muted" style={{ textAlign: 'right' }}>{Number(w.embedded).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  );
}

function Metric({
  label, value, sub, tone = '', spark,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: '' | 'ok' | 'warn' | 'err';
  spark?: { x: number; v: number }[];
}) {
  return (
    <div className={`metric ${tone}`}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
      {sub && <div className="metric-sub">{sub}</div>}
      {spark && spark.length > 1 && (
        <div className="metric-spark">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={spark} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
              <defs>
                <linearGradient id="g-spark" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%"   stopColor="#6aa6ff" stopOpacity={0.55} />
                  <stop offset="100%" stopColor="#6aa6ff" stopOpacity={0}    />
                </linearGradient>
              </defs>
              <Area type="monotone" dataKey="v" stroke="#6aa6ff" strokeWidth={1.5} fill="url(#g-spark)" />
              <Tooltip
                contentStyle={{ display: 'none' }}
                cursor={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

function Empty({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="empty">
      <div className="empty-icon">∅</div>
      <div style={{ color: 'var(--fg-0)', fontWeight: 500 }}>{title}</div>
      <div>{hint}</div>
    </div>
  );
}
