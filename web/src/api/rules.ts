import { j, apiFetch } from './core';

// ── Rule / Memory / Feedback capture transparency ──────────────────────────
export type AppliedFlag = {
  name: string;
  scope: string;
  repo?: string | null;
  session_id?: string | null;
};

export type CapturedRule = {
  id: string;
  category: string;
  scope: string;
  canonical?: string;
  text?: string;
  repo?: string | null;
  applied_flags?: AppliedFlag[];
};

export function rules(params?: { repo?: string; session_id?: number }):
  Promise<{ items: CapturedRule[]; by_scope: Record<string, CapturedRule[]> }> {
  const qs = new URLSearchParams();
  if (params?.repo) qs.set('repo', params.repo);
  if (params?.session_id != null) qs.set('session_id', String(params.session_id));
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  return j(`/rules${suffix}`);
}

export function setRuleScope(id: string, scope: string): Promise<any> {
  return j(`/rules/${id}/scope`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scope }),
  });
}

export function deleteRule(id: string): Promise<{ ok: boolean }> {
  return apiFetch(`/rules/${id}`, { method: 'DELETE' })
    .then(r => r.ok ? r.json() : { ok: false })
    .catch(() => ({ ok: false }));
}

// ── Explicit gate-disable flags (auto-approvals) ───────────────────────────
// A gate is only ever disabled by an explicit user action through these — never
// by the classifier. The capture pill merely OFFERS the opt-in.
export type GateFlags = {
  by_scope: {
    global: Record<string, boolean>;
    repo: Record<string, Record<string, boolean>>;
    session: Record<string, Record<string, boolean>>;
  };
};

export function ruleFlags(): Promise<GateFlags> {
  return j('/rules/flags');
}

export function setGateFlag(
  name: string, scope: string,
  opts?: { repo?: string; session_id?: number; rule_id?: string; allow_global?: boolean },
): Promise<{ ok: boolean; applied: boolean; reason?: string }> {
  return j('/rules/flags', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, scope, ...opts }),
  });
}

export function clearGateFlag(
  name: string, scope: string, opts?: { repo?: string; session_id?: number },
): Promise<{ ok: boolean }> {
  const qs = new URLSearchParams({ scope });
  if (opts?.repo) qs.set('repo', opts.repo);
  if (opts?.session_id != null) qs.set('session_id', String(opts.session_id));
  return apiFetch(`/rules/flags/${encodeURIComponent(name)}?${qs.toString()}`,
    { method: 'DELETE' })
    .then(r => r.ok ? r.json() : { ok: false })
    .catch(() => ({ ok: false }));
}
