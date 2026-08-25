import { MemoryStoreSection } from '../api';
import { SearchGroups } from './Memory.types';

export const KIND_OPTIONS = [
  { value: 'repo',  label: 'Code repo' },
  { value: 'docs',  label: 'Docs folder' },
  { value: 'url',   label: 'URL' },
  { value: 'file',  label: 'File upload' },
];

export function statusClass(status: string) {
  if (status === 'indexing') return 'source-status-indexing';
  if (status === 'done')     return 'source-status-done';
  if (status === 'error')    return 'source-status-error';
  return 'source-status-idle';
}

export function relativeDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diff = Date.now() - d.getTime();
  const mins  = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days  = Math.floor(diff / 86400000);
  if (mins < 1)   return 'just now';
  if (mins < 60)  return `${mins}m ago`;
  if (hours < 24) return `${hours}h ago`;
  return `${days}d ago`;
}

export function truncate(s: string, n = 50): string {
  return s.length > n ? s.slice(0, n) + '…' : s;
}

// ─── Memory overview + per-datasource clear ──────────────────────────────────

// One row per clearable datasource. `summary` turns the store's section into a
// one-line "what it has" string; a section that is `available:false` renders as
// unavailable (e.g. the graph stores when running on the SQLite backend).
export const OVERVIEW_STORES: {
  key: string;
  label: string;
  hint: string;
  summary: (s: MemoryStoreSection) => string;
}[] = [
  {
    key: 'sqlite', label: 'SQLite memory',
    hint: 'embedded units (learnings / failures / notes)',
    summary: s => {
      const total = (s.total ?? 0).toLocaleString();
      const kinds = Object.entries(s.by_kind || {})
        .map(([k, v]) => `${k} ${v}`).join(', ');
      return `${total} units` + (kinds ? ` — ${kinds}` : '');
    },
  },
  {
    key: 'md_files', label: 'Markdown notes',
    hint: 'human-readable .md memories on disk',
    summary: s => `${(s.count ?? 0).toLocaleString()} files` +
      (s.bytes ? ` · ${(s.bytes / 1024).toFixed(1)} KB` : ''),
  },
  {
    key: 'chat', label: 'Chat sessions',
    hint: 'saved conversations',
    summary: s => `${(s.sessions ?? 0).toLocaleString()} sessions, ` +
      `${(s.messages ?? 0).toLocaleString()} messages`,
  },
];

// ── memory files → user-facing CATEGORIES (Tasks / Solutions / Workflows /
// Commands / Topics), derived from kind + name + tags. One place, so the flat
// "compacted-*" dump becomes a browsable, grouped library.
export const CATEGORY_ORDER = ['Workflows', 'Commands', 'Solutions', 'Tasks', 'Topics'] as const;
export type Category = typeof CATEGORY_ORDER[number];

export function categoryOf(f: any): Category {
  const kind = String(f.kind || '').toLowerCase();
  const name = String(f.name || '').toLowerCase();
  const tags = new Set<string>((f.tags || []).map((t: string) => String(t).toLowerCase()));
  const has = (...xs: string[]) => xs.some(x => kind === x || tags.has(x));
  if (has('workflow') || name.startsWith('compacted-session-') || tags.has('workflow')) return 'Workflows';
  if (has('command') || tags.has('command') || tags.has('commands')) return 'Commands';
  if (has('decision', 'gotcha', 'bug', 'solution', 'fix', 'feedback', 'learning', 'project_learning')) return 'Solutions';
  if (has('task', 'session', 'project') || /^compacted-(jira|clr|rsp|\d)/.test(name)) return 'Tasks';
  return 'Topics';
}

// "compacted-sync-retry-policy" → "sync retry policy"; keeps a real title as-is.
export function cleanTitle(f: any): string {
  const t = String(f.title || f.name || '').replace(/\.md$/, '');
  return t.replace(/^compacted-/, '').replace(/-/g, ' ').trim() || t;
}

export const CAT_ICON: Record<Category, string> = {
  Workflows: '🔧', Commands: '⌨️', Solutions: '💡', Tasks: '📋', Topics: '🧭',
};

export const OKF_TYPE_BADGE: Record<string, string> = {
  objective: '🎯', key_result: '📊', learning: '🧠', session: '📎',
};

export const SEARCH_GROUPS: { key: keyof SearchGroups; label: string; hint: string }[] = [
  { key: 'vector', label: 'Vector index', hint: 'semantic nearest-neighbour (sqlite-vec)' },
  { key: 'md', label: 'Markdown files', hint: 'keyword / BM25 + linked briefs' },
  { key: 'other', label: 'Other sources', hint: 'bundle · ticket · graph' },
];
