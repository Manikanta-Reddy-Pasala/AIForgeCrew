// ── scheduled job types ───────────────────────────────────────────

export interface JobDraft {
  name: string;
  cron: string;
  ticket_title: string;
  ticket_body: string;
  project: string | null;
  // 'ticket' (default) → code pipeline → PR. 'agent' → runs the task through
  // the chat agent (jira/confluence/email tools, no code framing).
  kind?: 'ticket' | 'agent';
}

export interface JobPreview {
  ok: boolean;
  error?: string;
  draft?: JobDraft;
  human_schedule?: string;
  next_runs?: string[];
}

export interface Job extends JobDraft {
  id: number;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string;
  last_error: string | null;
  created_at: string;
  human_schedule: string;
  /** When the job closes itself (learning + scripts kept, row deleted).
   *  null = never; chat-created loops default to two hours. */
  expires_at: string | null;
}
