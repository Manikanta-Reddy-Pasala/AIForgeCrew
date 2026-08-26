// ── memory source types ───────────────────────────────────────────

export interface MemorySource {
  id: number;
  kind: string;
  name: string;
  location: string;
  status: string;
  units: number;
  error: string | null;
  last_indexed: string | null;
  created_at: string;
}

// ── memory overview (admin) types ─────────────────────────────────

export interface MemoryStoreSection {
  available?: boolean;
  reason?: string;
  // graph stores
  labels?: Record<string, number>;
  total?: number;
  relationships?: Record<string, number>;
  count?: number;
  // sqlite
  by_kind?: Record<string, number>;
  path?: string;
  // md
  bytes?: number;
  dir?: string;
  // chat
  sessions?: number;
  messages?: number;
  // sources
  by_status?: Record<string, number>;
  items?: MemorySource[];
}

export interface MemoryOverview {
  backend: string;
  stores: Record<string, MemoryStoreSection>;
  neo4j_browser?: string | null;
  neo4j_bolt?: string | null;
  neo4j_connect?: string | null;
  neo4j_user?: string | null;
}


// ── memory sync ──────────────────────────────────────────────────────────

/** ``state`` is what the settings panel reads to decide what to show. */
export type SyncState =
  | 'ok'
  /** Nobody picked, so the admin's first group was taken. Sync proceeds. */
  | 'group-defaulted'
  | 'unreachable'
  | 'group-unknown'
  | 'no-admin'
  | 'unknown';

/** One filter decision. The node's TEXT is never carried — it may be the secret. */
export interface SyncBlock {
  key: string;
  rule: string;
  reason: string;
  at: number;
}

export interface SyncStatus {
  state: SyncState;
  role: string;
  admin: string;
  group: string;
  groups_available: string[];
  reachable: boolean;
  /** Offered and not yet acknowledged. Recomputed each cycle, never queued. */
  pending: number;
  pushed_total: number;
  blocked: Record<string, number>;
  last_ok: number | null;
  last_error: string | null;
  /** True when AIFORGE_ADMIN_URL / AIFORGE_SYNC_GROUP is pinned in .env, in
   *  which case an edit here would be ignored and the field is read-only. */
  admin_pinned: boolean;
  group_pinned: boolean;
  rules: { stage: string; doc: string }[];
  recent_blocks: SyncBlock[];
}

export interface SyncRow {
  admin: string;
  group: string;
  state: string;
  ok: boolean;
  pushed: number;
  applied: number;
  rejected: number;
  conflicts: number;
  blocked: number;
  pending: number;
}
