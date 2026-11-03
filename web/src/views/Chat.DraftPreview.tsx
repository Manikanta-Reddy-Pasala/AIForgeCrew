/**
 * A Jira ticket or Confluence page the agent is about to write, shown IN THE
 * CHAT first — before the Approve/Reject pop-up.
 *
 * The pop-up is the right place for the final yes, but a poor place to read a
 * whole page body: it is pinned over the composer, capped in height, and the
 * buttons sit above the content. So for these tools the draft is posted into
 * the transcript like a reply, with one question under it. The user answers
 * the way they answer anything else — a button, or by typing in the composer:
 * "yes" moves on to the pop-up, anything else goes back to the agent as the
 * change to make (a reject with guidance, so it revises and shows the new
 * draft here again).
 *
 * Nothing here runs the tool. The server is still blocked on the same approval
 * request; this card only decides when the pop-up for it appears.
 */
import { useEffect, useRef } from 'react';
import { MdLite } from '../mdlite';
import type { PendingApproval } from './Chat.types';

// Tool → what the user is looking at, in their words.
const DRAFT_LABELS: Record<string, string> = {
  jira_create: 'Jira ticket',
  jira_update: 'Jira ticket change',
  jira_comment: 'Jira comment',
  jira_transition: 'Jira status change',
  jira_assign: 'Jira assignment',
  jira_link_issues: 'Jira link',
  jira_log_work: 'Jira work log',
  jira_dashboard_create: 'Jira dashboard',
  confluence_create: 'Confluence page',
  confluence_update: 'Confluence page change',
  confluence_comment: 'Confluence comment',
  confluence_add_label: 'Confluence label',
  confluence_attach: 'Confluence attachment',
};

/** Whether this approval shows its draft in the chat before the pop-up. */
export function previewInChatFirst(name: string, preview?: string): boolean {
  return !!preview && /^(jira|confluence)_/.test(name);
}

export function draftLabel(name: string): string {
  return DRAFT_LABELS[name]
    || (name.startsWith('jira_') ? 'Jira change' : 'Confluence change');
}

// A reply made ONLY of yes words and filler ("yes please", "ok, create it",
// "sounds good, go ahead") means go ahead. One word outside the set ("ok,
// assign it to Ravi", "correct the typo") makes it a change: a change read as
// a yes would be dropped and the old draft approved, while a yes read as a
// change only costs the agent one more round.
const YES_WORDS = new Set([
  'y', 'yes', 'yep', 'yeah', 'yup', 'ok', 'okay', 'k', 'sure', 'lgtm', 'proceed',
  'confirm', 'confirmed', 'approve', 'approved', 'accept', 'accepted', 'go',
  'ahead', 'do', 'it', 'create', 'publish', 'post', 'send', 'ship', 'looks',
  'look', 'sounds', 'good', 'fine', 'great', 'perfect', 'correct', 'right',
  'all', 'please', 'thanks', 'thank', 'you', 'that', 'thats', 'is', 'its',
  'lets', 'for', 'and', 'the', 'this', 'now', 'ticket', 'page', 'comment',
]);
const CORE_YES = new Set([
  'y', 'yes', 'yep', 'yeah', 'yup', 'ok', 'okay', 'k', 'sure', 'lgtm', 'proceed',
  'confirm', 'confirmed', 'approve', 'approved', 'accept', 'accepted', 'go',
  'do', 'create', 'publish', 'post', 'send', 'ship', 'good', 'fine', 'great',
  'perfect', 'correct',
]);
const YES_EMOJI = /^\s*(👍|✅|👌|✔️?)+\s*$/u;
const NO_ONLY = /^\s*(n|no|nope|nah|cancel|stop|abort|dont|don't|never ?mind|forget it|👎)\s*[.!]*\s*$/iu;

/** A composer reply that means "go ahead". Anything else is a change. */
export function isAffirmative(text: string): boolean {
  if (YES_EMOJI.test(text)) return true;
  const words = text.toLowerCase().replace(/['\u2018\u2019]/g, '')
    .split(/[^a-z]+/).filter(Boolean);
  return words.length > 0 && words.length <= 10
    && words.every(w => YES_WORDS.has(w))
    && words.some(w => CORE_YES.has(w));
}

/** A bare "no" / "cancel": stop, the same as the Cancel button. */
export function isNegative(text: string): boolean {
  return NO_ONLY.test(text);
}

export function DraftPreview({ p, onYes, onCancel }: Readonly<{
  p: PendingApproval;
  onYes: () => void;
  onCancel: () => void;
}>) {
  const label = draftLabel(p.name);
  const ref = useRef<HTMLDivElement>(null);
  // A new draft lands at the foot of a transcript that may be scrolled up.
  useEffect(() => {
    const calm = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    ref.current?.scrollIntoView({ block: 'nearest', behavior: calm ? 'auto' : 'smooth' });
  }, [p.id]);

  return (
    <div ref={ref} className="card" role="region"
         aria-label={`Draft ${label} to check`} style={{ margin: '8px 0', padding: 12 }}>
      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 8 }}>
        Here is the {label} I am about to write. Please check it.
      </div>
      <div tabIndex={0} style={{ fontSize: 13, background: 'var(--bg-2)', padding: 10,
                    borderRadius: 6, maxHeight: '70vh', overflow: 'auto' }}>
        <MdLite text={p.preview || ''} />
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center',
                    flexWrap: 'wrap', marginTop: 10 }}>
        <span aria-live="polite" style={{ fontSize: 13 }}>Shall I go ahead?</span>
        <button type="button" onClick={onYes}
                aria-label={`Yes, go ahead with this ${label}`}>Yes</button>
        <button type="button" className="ghost" onClick={onCancel}
                aria-label={`Cancel this ${label}`}>Cancel</button>
        <span className="muted xs">
          Or type what to change below — I will redo the draft.
        </span>
      </div>
    </div>
  );
}
