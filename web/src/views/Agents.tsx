import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';

export default function Agents() {
  const [agents, setAgents] = useState<any[]>([]);
  useEffect(() => { api.agents().then(setAgents); }, []);
  return (
    <>
      <h1>Agents</h1>
      <div className="grid grid-2">
        {agents.map(a => (
          <div key={a.role} className="card">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <h2 style={{ margin: 0 }}>{a.role}</h2>
              <span className="chip">{a.transport}</span>
            </div>
            <div className="small muted" style={{ marginTop: '.25rem' }}>
              model: <code>{a.model}</code> · max turns: {a.max_turns}
            </div>
            <div className="small muted" style={{ marginTop: '.25rem' }}>
              last activity: {a.last_activity || 'never'}
            </div>
            <div className="small muted" style={{ marginTop: '.25rem' }}>
              lifetime turns: {a.lifetime_turns}
            </div>
            <div style={{ marginTop: '.5rem' }}>
              <b>Tools:</b>{' '}
              {a.tool_allowlist.map((t: string) => (
                <span key={t} className="chip" style={{ marginRight: '.25rem' }}>{t}</span>
              ))}
            </div>
            {a.active_tickets.length > 0 && (
              <div style={{ marginTop: '.5rem' }}>
                <b>Active:</b>{' '}
                {a.active_tickets.map((t: any) => (
                  <Link key={t.identifier} to={`/tickets/${t.identifier}`}
                        style={{ marginRight: '.5rem' }}>
                    {t.identifier} ({t.status})
                  </Link>
                ))}
              </div>
            )}
            <div style={{ marginTop: '.5rem' }}>
              <Link to={`/logs/${a.role}`}>→ live logs</Link>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
