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
