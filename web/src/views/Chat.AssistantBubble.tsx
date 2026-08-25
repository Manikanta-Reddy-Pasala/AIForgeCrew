import { useState } from 'react';
import { toast } from 'sonner';
import { MdLite, copyText as mdCopyText } from '../mdlite';
import { AgentStep, SubtaskItem, CapturedItem } from './Chat.types';
import { fmtElapsed } from './Chat.helpers';
import { SubtaskList } from './Chat.SubtaskList';
import { CapturedPill } from './Chat.CapturedPill';
import { AgentStepRow } from './Chat.AgentSteps';

// ── AssistantBubble — renders steps + final text ──────────────────────────────

export function AssistantBubble({
  text,
  steps,
  streaming,
  elapsedSec,
  subtasks,
  captured,
  onRegenerate,
  stopped,
  onRerunFresh,
}: {
  text: string;
  steps: AgentStep[];
  streaming: boolean;
  elapsedSec?: number;
  subtasks?: SubtaskItem[];
  captured?: CapturedItem[];
  onRegenerate?: () => void;
  /** the turn ended on a stop — offer Resume instead of Regenerate */
  stopped?: boolean;
  /** discard the partial work and run the request again from nothing */
  onRerunFresh?: () => void;
}) {
  // Agent steps collapse by default once the turn is done (keeps the chat
  // clean — the plan/subtasks + final answer are what matter); auto-expanded
  // while streaming so the live flow is visible.
  const [showSteps, setShowSteps] = useState(streaming);
  // The Changes diff is the DELIVERABLE — pull it OUT of the collapsible steps so
  // it's always visible (thoughts/tools stay collapsed once the turn is done).
  const changeSteps = steps.filter(s => s.kind === 'changes');
  const otherSteps = steps.filter(s => s.kind !== 'changes');
  return (
    <div>
      {captured && captured.map(c => <CapturedPill key={c.id} item={c} />)}
      {subtasks && subtasks.length > 0 && <SubtaskList items={subtasks} />}
      {otherSteps.length > 0 && (
        <div style={{ marginBottom: text ? 8 : 0 }}>
          <button type="button"
            onClick={() => setShowSteps(v => !v)}
            style={{
              background: 'transparent', border: '1px solid var(--border-1)',
              borderRadius: 4, padding: '2px 8px', fontSize: 'var(--fs-xs)',
              color: 'var(--fg-3)', cursor: 'pointer', marginBottom: showSteps ? 6 : 0,
            }}
          >
            {showSteps ? '▾' : '▸'} {otherSteps.length} agent step{otherSteps.length === 1 ? '' : 's'}
          </button>
          {showSteps && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {otherSteps.map((s, i) => (
                <AgentStepRow key={i} step={s} />
              ))}
            </div>
          )}
        </div>
      )}
      {text && (
        <div className="bubble-body">
          <MdLite text={text} />
        </div>
      )}
      {/* Changes diff — always visible (the deliverable), below the answer. */}
      {changeSteps.length > 0 && (
        <div style={{ marginTop: text ? 8 : 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {changeSteps.map((s, i) => <AgentStepRow key={`chg-${i}`} step={s} />)}
        </div>
      )}
      {streaming && !text && steps.length === 0 && (
        <div className="bubble-body" style={{ padding: 0, background: 'transparent', border: 0 }}>
          <div className="typing"><span /><span /><span /></div>
        </div>
      )}
      {streaming && (steps.length > 0 || text) && (
        <div style={{ marginTop: 4, padding: '0 2px' }}>
          <div className="typing" style={{ padding: '4px 0' }}><span /><span /><span /></div>
        </div>
      )}
      {(elapsedSec !== undefined || (!streaming && (text || onRegenerate))) && (
      <div style={{
        marginTop: 6, fontSize: 'var(--fs-xs)', color: 'var(--fg-3)',
        display: 'flex', alignItems: 'center', gap: 8,
        fontVariantNumeric: 'tabular-nums',
      }}>
        {elapsedSec !== undefined && (streaming
          ? <span>⏱ {fmtElapsed(elapsedSec)}</span>
          : <span className="muted xs">· {fmtElapsed(elapsedSec)}</span>)}
        {/* M2: copy the assistant's answer */}
        {!streaming && text && (
          <button type="button" className="ghost xs" title="Copy the full answer"
                  style={{ padding: '0 4px', cursor: 'pointer' }}
                  onClick={() => mdCopyText(text).then(
                    () => toast.success('Copied'), () => toast.error('Copy failed'))}>
            ⧉ Copy
          </button>
        )}
        {!streaming && onRegenerate && (
          // A turn that was STOPPED (runaway cap, deadline, Stop button) is not
          // re-run, it is CONTINUED: the server carries over what already
          // landed and asks only for the remainder. Say which one this is, or
          // the user cannot tell the retry from a repeat.
          <button type="button" className="ghost xs"
                  title={stopped
                    ? 'Continue the stopped run — keeps the work that already landed and finishes what is pending'
                    : 'Re-run the previous request'}
                  style={{ padding: '0 4px', cursor: 'pointer' }}
                  onClick={onRegenerate}>
            {stopped ? '▸ Resume' : '↻ Regenerate'}
          </button>
        )}
        {!streaming && onRerunFresh && (
          <button type="button" className="ghost xs"
                  title="Ignore what the stopped run produced and start this request over"
                  style={{ padding: '0 4px', cursor: 'pointer' }}
                  onClick={onRerunFresh}>
            ↻ Start over
          </button>
        )}
      </div>
      )}
    </div>
  );
}
