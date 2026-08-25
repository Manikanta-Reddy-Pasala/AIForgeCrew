import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  DndContext, DragEndEvent, DragOverEvent, DragOverlay, DragStartEvent,
  PointerSensor, useSensor, useSensors, useDroppable, closestCorners,
} from '@dnd-kit/core';
import { useDraggable } from '@dnd-kit/core';
import { toast } from 'sonner';
import { api } from '../api';
import { Icon } from '../icons';
import { priorityClass, relTime } from '../util';

const COLUMNS: { key: string; title: string; color: string }[] = [
  { key: 'todo',        title: 'To do',        color: '#8892a0' },
  { key: 'in_progress', title: 'In progress',  color: '#6aa6ff' },
  { key: 'in_review',   title: 'In review',    color: '#a48bff' },
  { key: 'qa',          title: 'QA',           color: '#36c5d0' },
  { key: 'qa_failed',   title: 'QA failed',    color: '#e5534b' },
  { key: 'blocked',     title: 'Blocked',      color: '#f0883e' },
  { key: 'done',        title: 'Done',         color: '#3fb950' },
  { key: 'cancelled',   title: 'Cancelled',    color: '#5a6472' },
];

const PRIO_ORDER: Record<string, number> = { urgent: 0, high: 1, medium: 2, low: 3 };

export default function Kanban() {
  const qc = useQueryClient();
  const [q, setQ] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [activeId, setActiveId] = useState<string | null>(null);
  const [overCol, setOverCol] = useState<string | null>(null);
  // Local optimistic status overrides keyed by ticket id
  const [optimistic, setOptimistic] = useState<Record<string | number, string>>({});

  const { data = [], refetch, isFetching } = useQuery({
    queryKey: ['tickets', 'kanban'],
    queryFn: () => api.tickets({ limit: '400' }),
  });

  const grouped = useMemo(() => {
    const g: Record<string, any[]> = {};
    for (const c of COLUMNS) g[c.key] = [];
    const qq = q.trim().toLowerCase();
    for (const t of data) {
      if (roleFilter && t.assignee_role !== roleFilter) continue;
      if (qq) {
        const hay = `${t.identifier} ${t.title} ${t.project || ''}`.toLowerCase();
        if (!hay.includes(qq)) continue;
      }
      const st = optimistic[t.id] || t.status;
      g[st] ??= [];
      g[st].push({ ...t, status: st });
    }
    for (const k of Object.keys(g)) {
      g[k].sort((a, b) =>
        (PRIO_ORDER[a.priority] ?? 9) - (PRIO_ORDER[b.priority] ?? 9) ||
        (b.updated_at || '').localeCompare(a.updated_at || ''));
    }
    return g;
  }, [data, q, roleFilter, optimistic]);

  // Defensive: surface any status the backend emits that isn't a known column
  // (e.g. a future status) in an extra lane instead of silently dropping it.
  const allColumns = useMemo(() => {
    const known = new Set(COLUMNS.map(c => c.key));
    const extra = Object.keys(grouped)
      .filter(k => !known.has(k) && (grouped[k] || []).length > 0)
      .map(k => ({ key: k, title: k, color: '#8892a0' }));
    return [...COLUMNS, ...extra];
  }, [grouped]);

  const roles = useMemo(() => {
    const s = new Set<string>();
    for (const t of data) if (t.assignee_role) s.add(t.assignee_role);
    // localeCompare: the default sort orders by UTF-16 code unit, so any
    // non-ASCII role name sorts to the wrong place.
    return ['', ...Array.from(s).sort((a, b) => a.localeCompare(b))];
  }, [data]);

  const active = useMemo(
    () => data.find((t: any) => String(t.id) === activeId) || null,
    [data, activeId],
  );

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }));

  function onDragStart(e: DragStartEvent) {
    setActiveId(String(e.active.id));
  }
  function onDragOver(e: DragOverEvent) {
    const overId = e.over?.id ? String(e.over.id) : null;
    if (!overId) { setOverCol(null); return; }
    const colKey = overId.startsWith('col:') ? overId.slice(4) : null;
    setOverCol(colKey);
  }
  async function onDragEnd(e: DragEndEvent) {
    const id = String(e.active.id);
    const overId = e.over?.id ? String(e.over.id) : null;
    setActiveId(null); setOverCol(null);
    if (!overId) return;
    const colKey = overId.startsWith('col:') ? overId.slice(4) : null;
    if (!colKey) return;
    const t = data.find((x: any) => String(x.id) === id);
    if (!t) return;
    const curStatus = optimistic[t.id] || t.status;
    if (curStatus === colKey) return;

    // optimistic
    setOptimistic(s => ({ ...s, [t.id]: colKey }));
    try {
      await api.patch(t.identifier, { status: colKey });
      toast.success(`${t.identifier} → ${colKey.replace('_', ' ')}`);
      qc.invalidateQueries({ queryKey: ['tickets'] });
    } catch (err: any) {
      setOptimistic(s => {
        const { [t.id]: _, ...rest } = s;
        return rest;
      });
      toast.error(`Move failed: ${err.message || err}`);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Board</h1>
          <div className="subtitle">Drag cards between lanes to transition ticket status.</div>
        </div>
        <div className="row">
          <div className="input-search" style={{ minWidth: 220 }}>
            <Icon.Search size={14} />
            <input placeholder="filter tickets…" value={q} onChange={e => setQ(e.target.value)} />
          </div>
          <select value={roleFilter} onChange={e => setRoleFilter(e.target.value)} style={{ width: 150 }}>
            {roles.map(r => <option key={r} value={r}>{r || 'all roles'}</option>)}
          </select>
          <button type="button" className="ghost" onClick={() => refetch()} disabled={isFetching}>
            <Icon.Refresh size={14} /> Refresh
          </button>
        </div>
      </div>

      <DndContext
        sensors={sensors}
        collisionDetection={closestCorners}
        onDragStart={onDragStart}
        onDragOver={onDragOver}
        onDragEnd={onDragEnd}
      >
        <div className="kanban-board">
          {allColumns.map(col => (
            <Column
              key={col.key}
              col={col}
              tickets={grouped[col.key] || []}
              over={overCol === col.key}
            />
          ))}
        </div>
        <DragOverlay dropAnimation={null}>
          {active ? <TicketCard t={active} className="drag-overlay" /> : null}
        </DragOverlay>
      </DndContext>
    </>
  );
}

function Column({
  col, tickets, over,
}: Readonly<{
  col: { key: string; title: string; color: string };
  tickets: any[];
  over: boolean;
}>) {
  const { setNodeRef } = useDroppable({ id: `col:${col.key}` });
  return (
    <div
      ref={setNodeRef}
      className={`kanban-col${over ? ' is-over' : ''}`}
      style={{ ['--col-accent' as any]: col.color }}
    >
      <div className="kanban-col-header">
        <span>{col.title}</span>
        <span className="count">{tickets.length}</span>
      </div>
      <div className="kanban-col-body">
        {tickets.length === 0 ? (
          <div className="empty" style={{ padding: '24px 8px' }}>
            <div className="xs">Empty</div>
          </div>
        ) : tickets.map(t => <DraggableCard key={t.id} t={t} />)}
      </div>
    </div>
  );
}

function DraggableCard({ t }: Readonly<{ t: any }>) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: String(t.id) });
  const navigate = useNavigate();
  // Track drag distance so a click that didn't move (>4px) opens the
  // detail page; a real drag does not. Pure pointer-onClick wouldn't
  // know if the user dragged or clicked, so we measure ourselves.
  let dragStart = { x: 0, y: 0 };
  return (
    <div
      ref={setNodeRef}
      {...attributes}
      {...listeners}
      onPointerDown={e => { dragStart = { x: e.clientX, y: e.clientY }; (listeners as any)?.onPointerDown?.(e); }}
      onClick={(e: any) => {
        const dx = Math.abs((e.clientX || 0) - dragStart.x);
        const dy = Math.abs((e.clientY || 0) - dragStart.y);
        if (dx + dy < 4 && !isDragging) {
          // Use react-router navigate so the /ui basename applies.
          // window.location.href would bypass it and 404.
          navigate(`/tickets/${t.identifier}`);
        }
      }}
      // Keyboard parity for the click-to-open above: the card is reachable via
      // dnd-kit's attributes (role=button, tabIndex=0), so Enter/Space must
      // open the detail page the same way a click does.
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          navigate(`/tickets/${t.identifier}`);
        }
      }}
      style={{ cursor: 'pointer' }}
    >
      <TicketCard t={t} dragging={isDragging} />
    </div>
  );
}

function TicketCard({
  t, dragging = false, className = '',
}: {
  t: any; dragging?: boolean; className?: string;
}) {
  return (
    <div className={`ticket-card ${priorityClass(t.priority)} ${dragging ? 'dragging' : ''} ${className}`}>
      <div className="tc-top">
        <Link to={`/tickets/${t.identifier}`} className="identifier-badge" onClick={e => e.stopPropagation()}>
          {t.identifier}
        </Link>
        <span className={`chip sm ${priorityClass(t.priority)}`}>{t.priority}</span>
      </div>
      <div className="tc-title">{t.title}</div>
      <div className="tc-meta">
        <div className="row tight">
          {(t.active_role || t.assignee_role) && (
            <span className="chip sm" title={t.active_role ? 'last active stage' : 'assignee'}>
              {t.active_role || t.assignee_role}
            </span>
          )}
          {Array.isArray(t.labels) && t.labels.slice(0, 3).map((l: string) => (
            <span key={l} className="chip sm">{l}</span>
          ))}
        </div>
        <span className="xs muted nowrap" title={t.updated_at}>{relTime(t.updated_at)}</span>
      </div>
    </div>
  );
}
