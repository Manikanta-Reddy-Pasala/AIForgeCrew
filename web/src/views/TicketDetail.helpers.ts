export const TRANSITIONS = ['todo', 'in_progress', 'in_review', 'qa', 'qa_failed', 'done', 'blocked', 'cancelled'];

export const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|bmp)$/i;

export function fmtSize(n?: number) {
  if (!n && n !== 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

// ── Subtask progress (Planner decomposition, tracked internally) ──────────────
export const SUBTASK_COLORS: Record<string, string> = {
  done: '#3fb950', skipped: '#5a6472', running: '#6aa6ff',
  failed: '#e5534b', pending: '#8892a0',
};
