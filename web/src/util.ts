/* Small UI utilities — pure, no deps. */

export function statusClass(s: string): string {
  if (s === 'done') return 'ok';
  if (s === 'blocked' || s === 'cancelled' || s === 'qa_failed') return 'err';
  if (s === 'in_progress' || s === 'in_review' || s === 'qa') return 'active';
  if (s === 'todo') return '';
  return '';
}

export function priorityClass(p: string): string {
  switch (p) {
    case 'urgent': return 'prio-urgent';
    case 'high':   return 'prio-high';
    case 'medium': return 'prio-medium';
    case 'low':    return 'prio-low';
    default:       return 'prio-medium';
  }
}

const TERMINAL = new Set(['done', 'cancelled', 'qa_failed']);

export function formatDuration(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return '—';
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), rs = s % 60;
  if (m < 60) return rs ? `${m}m ${rs}s` : `${m}m`;
  const h = Math.floor(m / 60), rm = m % 60;
  if (h < 24) return rm ? `${h}h ${rm}m` : `${h}h`;
  const d = Math.floor(h / 24), rh = h % 24;
  return rh ? `${d}d ${rh}h` : `${d}d`;
}

export function durationCell(t: any): string {
  if (!t.started_at) return t.status === 'todo' ? '—' : '…';
  const live = !TERMINAL.has(t.status);
  return live ? `${formatDuration(t.duration_s)} ⏱` : formatDuration(t.duration_s);
}

export function durationTitle(t: any): string {
  if (!t.started_at) return 'never entered in_progress';
  const live = !TERMINAL.has(t.status);
  const base = `started ${(t.started_at || '').slice(0, 19).replace('T', ' ')}`;
  return live ? `${base} (running)` : `${base} → ${(t.completed_at || '').slice(0, 19).replace('T', ' ') || 'end'}`;
}

/** Relative time: "2m ago", "3h ago", "yesterday", "5d ago". */
export function relTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const diff = Math.max(0, Date.now() - t) / 1000;
  if (diff < 45) return 'just now';
  if (diff < 90) return '1m ago';
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 7200) return '1h ago';
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  if (diff < 172800) return 'yesterday';
  if (diff < 86400 * 30) return `${Math.round(diff / 86400)}d ago`;
  if (diff < 86400 * 365) return `${Math.round(diff / 86400 / 30)}mo ago`;
  return `${Math.round(diff / 86400 / 365)}y ago`;
}

export function initials(s: string): string {
  if (!s) return '?';
  const parts = s.split(/[\s_-]+/).filter(Boolean);
  if (parts.length === 0) return s[0]?.toUpperCase() || '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

/** Extract "ONE" from "ONE-46", etc. */
export function identifierPrefix(id: string): string {
  const m = /^([A-Z]+)-/.exec(id || '');
  return m ? m[1] : '';
}
