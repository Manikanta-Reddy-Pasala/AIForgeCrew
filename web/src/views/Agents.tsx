import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { Icon } from '../icons';
import { relTime } from '../util';

export default function Agents() {
  const { data: agents = [], isLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: () => api.agents(),
  });

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Agents</h1>
          <div className="subtitle">Roster of registered agent roles, their models, and current load.</div>
        </div>
      </div>

      {isLoading && <div className="skeleton" style={{ height: 120 }} />}

      <div className="grid grid-2">
        {agents.map(a => (
          <div key={a.role} className="card hover">
            <div className="card-header">
              <div className="row">
                <h2 style={{ margin: 0 }}>{a.role}</h2>
                <span className="chip">{a.transport}</span>
              </div>
              <Link to={`/logs/${a.role}`} className="chip info">
                <Icon.Logs size={12} /> live logs
              </Link>
            </div>
            <div className="stack">
              <div className="row">
                <span className="muted small">model</span>
                <code style={{ fontSize: 12 }}>{a.model}</code>
              </div>
              <div className="row">
                <span className="muted small">max turns</span>
                <span className="small mono">{a.max_turns}</span>
                <span className="sep" />
                <span className="muted small">lifetime</span>
                <span className="small mono">{a.lifetime_turns}</span>
                <span className="sep" />
                <span className="muted small">last</span>
                <span className="small">{relTime(a.last_activity)}</span>
              </div>

              {a.tool_allowlist?.length > 0 && (
                <div>
                  <div className="muted xs" style={{ marginBottom: 4, textTransform: 'uppercase', letterSpacing: '.05em' }}>Tools</div>
                  <div className="row tight">
                    {a.tool_allowlist.map((t: string) => (
                      <span key={t} className="chip sm mono">{t}</span>
                    ))}
                  </div>
                </div>
              )}

              {a.active_tickets?.length > 0 && (
                <div>
                  <div className="muted xs" style={{ marginBottom: 4, textTransform: 'uppercase', letterSpacing: '.05em' }}>
                    Active ({a.active_tickets.length})
                  </div>
                  <div className="row tight">
                    {a.active_tickets.map((t: any) => (
                      <Link
                        key={t.identifier}
                        to={`/tickets/${t.identifier}`}
                        className="chip"
                      >
                        {t.identifier} · {t.status}
                      </Link>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
