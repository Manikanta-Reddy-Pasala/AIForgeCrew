// ── Chat projects + per-project memory ────────────────────────────
import { j } from './core';

export interface Project {
  name: string;
  path: string;
  is_git: boolean;
  has_aiforge: boolean;
  key: string;
  slug: string;
  registered: boolean;
  memory_chars: number;
  memory_cap: number;
  stale: number;
  writable: boolean;
  source?: string;        // the mounted folder it was found in
  mine?: boolean;         // on "your projects": opened before, not taken off
  opened_at?: number;
  chats?: number;
  last_activity?: string;
}

export interface ProjectList {
  root: string;
  exists: boolean;
  projects: Project[];
  roots?: { path: string; kind: 'projects' | 'mount' }[];
  no_project?: { chats: number; last_activity: string };
}

/** A folder suggested while typing a path. `openable` false = only a step on
 *  the way to a folder that can be opened. */
export interface FolderHit {
  name: string;
  path: string;
  is_git: boolean;
  openable: boolean;
}

export interface StaleFact {
  fact: string;
  missing?: string[];
  at?: number;
}

export interface ProjectMemory {
  name: string;
  key: string;
  slug: string;
  path: string;
  text: string;
  chars: number;
  cap: number;
  writable: boolean;
  repo_file: string;
  compacted_at?: number | null;
  synced_at?: number | null;
  stale: StaleFact[];
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };
const enc = encodeURIComponent;

export const projectsApi = {
  list: () => j<ProjectList>('/projects'),
  open: (name: string) => j<Project>(`/projects/${enc(name)}/open`, { method: 'POST' }),
  openPath: (path: string) => j<Project>('/projects/open', {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ path }),
  }),
  remove: (path: string) => j<{ ok: boolean }>('/projects/remove', {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ path }),
  }),
  browse: (q: string, signal?: AbortSignal) =>
    j<{ q: string; folders: FolderHit[] }>(`/projects/browse?q=${enc(q)}`, { signal }),

  withMemory: () => j<Project[]>('/memory/projects'),
  memory: (name: string) => j<ProjectMemory>(`/memory/projects/${enc(name)}`),
  saveMemory: (name: string, text: string) =>
    j<{ ok: boolean; chars: number }>(`/memory/projects/${enc(name)}`, {
      method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ text }),
    }),
  compact: (name: string) =>
    j<{ ok: boolean; compacted?: boolean; stale?: number; error?: string;
        chars_before?: number; chars_after?: number }>(
      `/memory/projects/${enc(name)}/compact`, { method: 'POST' }),
  promote: (name: string, text: string) =>
    j<{ ok: boolean; moved: number }>(`/memory/projects/${enc(name)}/promote`, {
      method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ text }),
    }),
  stale: (name: string, fact: string, action: 'restore' | 'delete') =>
    j<{ ok: boolean }>(`/memory/projects/${enc(name)}/stale`, {
      method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ fact, action }),
    }),
  forget: (name: string) =>
    j<{ ok: boolean; archived?: string }>(`/memory/projects/${enc(name)}`, { method: 'DELETE' }),
};
