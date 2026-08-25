import { useContext, useState } from 'react';
import { toast } from 'sonner';
import { setRuleScope, deleteRule, setGateFlag } from '../api';
import { CapturedItem, RuleStateCtx } from './Chat.types';
import { GATE_INTENT_FLAG, GATE_INTENT_LABEL, FLAG_LABEL } from './Chat.helpers';

// ── CapturedPill — inline "Saved RULE · scope" note (change-scope / undo) ─────
//
// A captured rule is REMEMBERED (rule book) on capture. If it ALSO looks like a
// request to stop asking before commits/deletes (`gate_intent`), the pill shows
// a DISTINCT, explicit opt-in to disable that gate for THIS session or THIS repo
// — never global (global needs the dedicated panel + confirm). The opt-in is the
// ONLY thing that disables a gate; capture itself never does.

export function CapturedPill({ item }: { item: CapturedItem }) {
  const rs = useContext(RuleStateCtx);
  // Hydrate from server truth so undo/rescope SURVIVE a reload: a persisted pill
  // whose id is gone from the index was deleted; otherwise use its current scope.
  const hydrated = rs?.byId[item.id];
  const wasDeleted = rs?.loaded && !hydrated && item.scope !== 'session';
  const scope = hydrated?.scope || item.scope;
  const appliedFlags = hydrated?.applied_flags || [];

  const [removed, setRemoved] = useState(false);
  const [busy, setBusy] = useState(false);
  if (removed || wasDeleted) return null;

  const flagName = item.gate_intent ? GATE_INTENT_FLAG[item.gate_intent] : '';
  const flagApplied = appliedFlags.some(f => f.name === flagName);

  async function changeScope(next: string) {
    if (next === scope || busy) return;
    setBusy(true);
    try {
      await setRuleScope(item.id, next);
      rs?.refresh();
    } catch { toast.error('Could not change scope'); }
    finally { setBusy(false); }
  }
  async function undo() {
    if (busy) return;
    setBusy(true);
    try {
      // DELETE clears the rule AND revokes any gate flag it enabled.
      const r = await deleteRule(item.id);
      if (r.ok) { setRemoved(true); rs?.refresh(); }
      else toast.error('Could not undo');
    } catch { toast.error('Could not undo'); }
    finally { setBusy(false); }
  }
  async function optIn(scopeKind: 'session' | 'project') {
    if (busy || !flagName) return;
    setBusy(true);
    try {
      const opts: { rule_id: string; session_id?: number; repo?: string } = { rule_id: item.id };
      if (scopeKind === 'session') {
        if (rs?.sessionId == null) { toast.error('No active session'); return; }
        opts.session_id = rs.sessionId;
      } else {
        if (!item.repo) { toast.error('No repo for this rule'); return; }
        opts.repo = item.repo;
      }
      const res = await setGateFlag(flagName, scopeKind, opts);
      if (res.applied) { toast.success('Gate disabled for this ' + (scopeKind === 'session' ? 'session' : 'repo')); rs?.refresh(); }
      else toast.error(res.reason || 'Could not enable');
    } catch { toast.error('Could not enable'); }
    finally { setBusy(false); }
  }

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
      border: '1px solid var(--border-1)', borderRadius: 6,
      padding: '5px 10px', margin: '4px 0', fontSize: 12,
      background: 'var(--bg-1,#0d1117)',
    }}>
      <span style={{ color: '#3fb950', fontWeight: 600 }}>✓ Saved</span>
      <span style={{
        fontFamily: 'var(--font-mono)', fontWeight: 600, textTransform: 'uppercase',
        fontSize: 10, padding: '1px 6px', borderRadius: 999,
        color: '#a371f7', border: '1px solid #a371f7',
      }}>{item.category}</span>
      {item.text && (
        <span style={{ color: 'var(--fg-2,#8892a0)', overflow: 'hidden',
          textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 320 }}
          title={item.text}>{item.text}</span>
      )}
      <span style={{ color: '#8892a0' }}>·</span>
      <select value={scope} disabled={busy} onChange={e => changeScope(e.target.value)}
        title="change scope"
        style={{ fontSize: 11, background: 'var(--bg-2,#161b22)',
          color: 'var(--fg-1)', border: '1px solid var(--border-1)',
          borderRadius: 4, padding: '1px 4px' }}>
        <option value="global">global</option>
        <option value="project">project</option>
        <option value="session">session</option>
      </select>
      <button type="button" onClick={undo} disabled={busy}
        style={{ marginLeft: 'auto', background: 'transparent',
          border: '1px solid var(--border-1)', borderRadius: 4,
          padding: '1px 8px', fontSize: 11, color: 'var(--fg-3)',
          cursor: busy ? 'default' : 'pointer' }}>undo</button>

      {/* Explicit gate-disable opt-in (only when the rule reads like one) */}
      {item.gate_intent && (
        <div style={{
          flexBasis: '100%', display: 'flex', alignItems: 'center', gap: 6,
          marginTop: 4, paddingTop: 4, borderTop: '1px dashed var(--border-1)',
          color: '#d4a72c', fontSize: 11,
        }}>
          {flagApplied ? (
            <span>⚠ {FLAG_LABEL[flagName]} (enabled — undo to revoke)</span>
          ) : (
            <>
              <span>⚠ {GATE_INTENT_LABEL[item.gate_intent]}</span>
              <button type="button" onClick={() => optIn('session')} disabled={busy}
                style={{ background: 'transparent', border: '1px solid #d4a72c',
                  borderRadius: 4, padding: '1px 8px', fontSize: 11,
                  color: '#d4a72c', cursor: busy ? 'default' : 'pointer' }}>
                This session</button>
              {item.repo && (
                <button type="button" onClick={() => optIn('project')} disabled={busy}
                  style={{ background: 'transparent', border: '1px solid #d4a72c',
                    borderRadius: 4, padding: '1px 8px', fontSize: 11,
                    color: '#d4a72c', cursor: busy ? 'default' : 'pointer' }}>
                  This repo</button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
