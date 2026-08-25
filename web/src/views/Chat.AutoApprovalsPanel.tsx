import { useContext } from 'react';
import { toast } from 'sonner';
import { clearGateFlag } from '../api';
import { RuleStateCtx } from './Chat.types';
import { FLAG_LABEL } from './Chat.helpers';

// ── AutoApprovalsPanel — active gate-disable flags, with Revoke ───────────────
// The audit surface: every disabled gate is visible and revocable here. No way
// to ENABLE a global flag from the UI — that stays a deliberate, separate step.

export function AutoApprovalsPanel() {
  const rs = useContext(RuleStateCtx);
  const flags = rs?.flags?.by_scope;
  if (!flags) return null;
  type Row = { name: string; scope: string; repo?: string; session?: string; label: string };
  const rows: Row[] = [];
  Object.keys(flags.global || {}).forEach(n => rows.push({ name: n, scope: 'global', label: 'global' }));
  Object.entries(flags.repo || {}).forEach(([repo, d]) =>
    Object.keys(d || {}).forEach(n => rows.push({ name: n, scope: 'project', repo, label: `repo ${repo}` })));
  Object.entries(flags.session || {}).forEach(([sid, d]) =>
    Object.keys(d || {}).forEach(n => rows.push({ name: n, scope: 'session', session: sid, label: `session ${sid}` })));
  if (rows.length === 0) return null;

  async function revoke(r: Row) {
    try {
      await clearGateFlag(r.name, r.scope,
        { repo: r.repo, session_id: r.session != null ? Number(r.session) : undefined });
      rs?.refresh();
    } catch { toast.error('Could not revoke'); }
  }

  return (
    <div style={{
      border: '1px solid #d4a72c', borderRadius: 6, padding: '6px 10px',
      margin: '6px 0', fontSize: 12, background: 'rgba(212,167,44,0.06)',
    }}>
      <div style={{ fontWeight: 600, color: '#d4a72c', marginBottom: 4 }}>
        ⚠ Auto-approvals active ({rows.length})
      </div>
      {rows.map((r) => (
        <div key={`${r.scope}:${r.name}:${r.label}`} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '2px 0' }}>
          <span style={{ color: 'var(--fg-2,#8892a0)' }}>
            {FLAG_LABEL[r.name] || r.name} · <span style={{ fontFamily: 'var(--font-mono)' }}>{r.label}</span>
          </span>
          <button type="button" onClick={() => revoke(r)}
            style={{ marginLeft: 'auto', background: 'transparent',
              border: '1px solid var(--border-1)', borderRadius: 4,
              padding: '1px 8px', fontSize: 11, color: 'var(--fg-3)', cursor: 'pointer' }}>
            Revoke</button>
        </div>
      ))}
    </div>
  );
}
