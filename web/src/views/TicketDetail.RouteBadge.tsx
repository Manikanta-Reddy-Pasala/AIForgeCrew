import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api, type WorkflowSpec } from '../api';

// ── Route badge ────────────────────────────────────────────────────
//
// Renders the current route (code task vs. workflow id) as a chip.
// Click → opens an inline override panel that lets the operator
// switch routes. Posts to PUT /api/tickets/{id}/route which records
// route_source='manual' so the change is auditable.

export function RouteBadge({ t, onChanged }: Readonly<{ t: any; onChanged: () => void }>) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [pickedKind, setPickedKind] = useState<'code' | 'workflow'>(
    t.route === 'workflow' ? 'workflow' : 'code',
  );
  const [pickedWorkflow, setPickedWorkflow] = useState<string>(
    t.route_workflow || '',
  );

  const { data: workflows = [] } = useQuery<WorkflowSpec[]>({
    queryKey: ['workflows'],
    queryFn: () => api.workflows(),
    staleTime: 60_000,
    enabled: open,
  });

  const isWorkflow = t.route === 'workflow' && t.route_workflow;
  const sourceLabel = t.route_source === 'manual' ? 'manual' : 'auto';
  const conf = typeof t.route_confidence === 'number'
    ? ` · ${t.route_confidence.toFixed(2)}` : '';

  async function save() {
    if (pickedKind === 'workflow' && !pickedWorkflow) {
      toast.error('Pick a workflow id'); return;
    }
    setPending(true);
    try {
      await api.setRoute(t.identifier, pickedKind,
        pickedKind === 'workflow' ? pickedWorkflow : undefined);
      toast.success(`Route → ${pickedKind === 'workflow' ? pickedWorkflow : 'code task'}`);
      setOpen(false);
      onChanged();
    } catch (e: any) {
      toast.error(`Override failed: ${e.message}`);
    } finally {
      setPending(false);
    }
  }

  return (
    <div style={{ position: 'relative' }}>
      <button type="button"
        className="chip"
        title={`Route source: ${sourceLabel}${conf}`}
        onClick={() => setOpen(o => !o)}
        style={{
          background: isWorkflow ? 'var(--accent-bg, #e8f4ff)' : 'transparent',
          border: '1px solid var(--border-1)',
          padding: '2px 8px',
          fontSize: 11,
          cursor: 'pointer',
        }}
      >
        {isWorkflow ? '⚙' : '📝'}{' '}
        {isWorkflow ? t.route_workflow : 'code task'}
        <span style={{ opacity: 0.6, marginLeft: 6 }}>[{sourceLabel}]</span>
      </button>
      {open && (
        <div className="card" style={{
          position: 'absolute', top: '100%', right: 0, marginTop: 6,
          padding: 12, minWidth: 320, zIndex: 50,
        }}>
          <div style={{ marginBottom: 8 }}><strong>Override route</strong></div>
          <div className="row" style={{ gap: 12, marginBottom: 8 }}>
            <label className="row" style={{ gap: 4 }}>
              <input type="radio"
                checked={pickedKind === 'code'}
                onChange={() => setPickedKind('code')} />
              Code task
            </label>
            <label className="row" style={{ gap: 4 }}>
              <input type="radio"
                checked={pickedKind === 'workflow'}
                onChange={() => setPickedKind('workflow')} />
              Workflow
            </label>
          </div>
          {pickedKind === 'workflow' && (
            <select
              value={pickedWorkflow}
              onChange={e => setPickedWorkflow(e.target.value)}
              style={{ width: '100%', marginBottom: 8 }}
            >
              <option value="">— pick a workflow —</option>
              {workflows.map(w => (
                <option key={w.id} value={w.id}>{w.label}</option>
              ))}
            </select>
          )}
          <div className="row" style={{ justifyContent: 'flex-end', gap: 8 }}>
            <button type="button" className="ghost" onClick={() => setOpen(false)}>Cancel</button>
            <button type="button" onClick={save} disabled={pending}>Save</button>
          </div>
        </div>
      )}
    </div>
  );
}
