import { createContext, useContext, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import { api, chatApi, chatSessionMessageURL, chatSessionAttachURL, chatSessionStop, chatSessionSteer, chatKillAll, chatMediaUpload, chatMediaList, chatMediaDescribe, chatMediaDelete, chatMediaRawURL, ChatMedia, chatSessionSpec, setRuleScope, deleteRule, rules as fetchRules, ruleFlags, setGateFlag, clearGateFlag, CapturedRule, GateFlags, ChatSession, ChatMsg, ChatModelEntry } from '../api';
import { Icon } from '../icons';
import { MdLite } from '../mdlite';

// Header overflow-menu item styles.
const menuBtn: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left',
  background: 'none', border: 'none', padding: '7px 10px', borderRadius: 6,
  fontSize: 13, color: 'var(--fg-1)', cursor: 'pointer',
};
const menuItem: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px',
  fontSize: 13, color: 'var(--fg-1)', cursor: 'pointer',
};

// ── types ──────────────────────────────────────────────────────────────────────

type ChangeFile = { path: string; status: string; additions: number; deletions: number; diff: string };

type AgentStep =
  | { kind: 'thought'; text: string; role?: string }
  | { kind: 'tool'; name: string; args: object; result: object; role?: string }
  | { kind: 'error'; text: string; role?: string }
  | { kind: 'changes'; files: ChangeFile[]; summary: { files: number; additions: number; deletions: number } };

type SubtaskItem = { slug: string; goal: string; status: string };

// A captured Rule / Memory / Feedback (deterministic capture pass). Rendered
// as an inline pill with change-scope / undo affordances.
type CapturedItem = {
  id: string;
  category: string;
  scope: string;
  text: string;
  repo?: string | null;
  // Set when the captured rule LOOKS like a gate-disable request — the pill then
  // OFFERS an explicit, scoped opt-in. A gate is NEVER disabled by capture.
  gate_intent?: 'commit' | 'delete';
};

// Shared, server-truth state for captured-rule pills so undo/rescope SURVIVE a
// reload (the persisted pill hydrates from this rather than its stale step).
type RuleState = {
  byId: Record<string, CapturedRule>;   // current persisted truth
  loaded: boolean;                       // has the index been fetched at least once
  sessionId: number | null;
  flags: GateFlags | null;               // active gate-disable flags
  refresh: () => void;
};
const RuleStateCtx = createContext<RuleState | null>(null);

// A "live" turn: the in-progress assistant turn while streaming.
type LiveTurn = {
  role: 'assistant';
  text: string;
  steps: AgentStep[];
  streaming: boolean;
  elapsedSec?: number;
  awaiting?: boolean;   // agent asked a question — waiting for your reply
  subtasks?: SubtaskItem[];   // Planner decomposition (team mode)
  captured?: CapturedItem[];  // Rule/Memory/Feedback captured this turn
  usage?: { pct: number; chars: number; budget: number; tokens?: number; windowTokens?: number };  // context-window fill
};

const SUBTASK_COLORS: Record<string, string> = {
  done: '#3fb950', skipped: '#5a6472', running: '#6aa6ff',
  failed: '#e5534b', pending: '#8892a0', planned: '#a371f7',
  won: '#d4a72c', cancelled: '#5a6472',
};

function SubtaskList({ items, onViewSpec }: { items: SubtaskItem[]; onViewSpec?: () => void }) {
  // Default COLLAPSED — the header line is the crisp, live at-a-glance view;
  // click to expand the full list.
  const [open, setOpen] = useState(false);
  const counts = items.reduce((m, s) => { m[s.status] = (m[s.status] || 0) + 1; return m; }, {} as Record<string, number>);
  const done = (counts['done'] || 0) + (counts['won'] || 0) + (counts['skipped'] || 0);
  const order = ['done', 'won', 'running', 'failed', 'cancelled', 'planned', 'pending', 'skipped'];
  // Live "current" subtask shown in the collapsed header so status reads in
  // real time without expanding.
  const current = items.find(s => s.status === 'running')
    || items.find(s => !['done', 'won', 'skipped', 'failed', 'cancelled'].includes(s.status));
  const pct = items.length ? Math.round((done / items.length) * 100) : 0;
  return (
    <div style={{ border: '1px solid var(--border-1)', borderRadius: 6, padding: '6px 10px', margin: '4px 0', background: 'var(--bg-1,#0d1117)' }}>
      <div onClick={() => setOpen(v => !v)}
        style={{ fontSize: 12, fontWeight: 600, marginBottom: open ? 6 : 0, cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10 }}>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {open ? '▾' : '▸'} Tasks <span style={{ color: '#8892a0' }}>{done}/{items.length}</span>
          {!open && current && (
            <span style={{ color: SUBTASK_COLORS[current.status] || SUBTASK_COLORS.running, fontWeight: 500, marginLeft: 8 }}>
              ▸ {current.slug}
            </span>
          )}
        </span>
        <span style={{ display: 'flex', gap: 6, fontSize: 10, fontWeight: 500, flexShrink: 0, alignItems: 'center' }}>
          {order.filter(k => counts[k]).map(k => (
            <span key={k} style={{ color: SUBTASK_COLORS[k] }}>● {counts[k]}</span>
          ))}
          <span style={{ color: '#8892a0' }}>{pct}%</span>
          {onViewSpec && (
            <button className="ghost xs" title="Preview SPEC.md (the plan's requirements)"
                    onClick={(e) => { e.stopPropagation(); onViewSpec(); }}
                    style={{ cursor: 'pointer', padding: '1px 6px', fontSize: 10, whiteSpace: 'nowrap' }}>
              📄 SPEC.md
            </button>
          )}
        </span>
      </div>
      {/* progress bar */}
      <div style={{ display: open ? 'flex' : 'none', height: 5, borderRadius: 3, overflow: 'hidden', background: 'var(--bg-2,#222)', marginBottom: 8 }}>
        {order.map(k => counts[k] ? <div key={k} style={{ width: `${(counts[k] / items.length) * 100}%`, background: SUBTASK_COLORS[k] }} /> : null)}
      </div>
      {open && items.map((s, i) => (
        <div key={s.slug || i} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, padding: '2px 0' }}>
          <span style={{ flexShrink: 0, width: 58, textAlign: 'center', fontSize: 10, fontWeight: 600,
            color: SUBTASK_COLORS[s.status] || SUBTASK_COLORS.pending,
            border: `1px solid ${SUBTASK_COLORS[s.status] || SUBTASK_COLORS.pending}`, borderRadius: 4, padding: '1px 3px' }}>{s.status}</span>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
            <span style={{ color: '#8892a0', fontFamily: 'monospace', marginRight: 6 }}>{s.slug}</span>
            <span style={{ color: '#8892a0' }}>{s.goal.length > 60 ? s.goal.slice(0, 60) + '…' : s.goal}</span>
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Elapsed time formatter ────────────────────────────────────────────────────

function fmtElapsed(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

const LS_SESSION_KEY = 'aiforge.chat.activeSessionId';
const LS_MODEL_KEY = 'aiforge.chat.model';
const LS_MODE_KEY = 'aiforge.chat.flowmode';

type ChatMode = 'simple' | 'plan' | 'team';

// ── Builder flows ─────────────────────────────────────────────────────────────
// A "builder" runs a focused single-agent interview that ends by calling a
// finalize tool. It's selected per-session and sent on EVERY message of that
// conversation (the backend reads it per-message). Launched from other views via
// a `?builder=<kind>` query param on /chat.
type BuilderKind = 'job' | 'skill' | 'workflow' | 'rule';
const BUILDER_KINDS: BuilderKind[] = ['job', 'skill', 'workflow', 'rule'];
const BUILDER_LABELS: Record<BuilderKind, string> = {
  job: 'Job builder',
  skill: 'Skill builder',
  workflow: 'Workflow builder',
  rule: 'Rule builder',
};
const BUILDER_HINTS: Record<BuilderKind, string> = {
  job: 'Interviewing you to build & schedule a recurring job',
  skill: 'Interviewing you to capture a reusable SKILL.md',
  workflow: 'Interviewing you to capture a WORKFLOW.md',
  rule: 'Interviewing you to capture a standing rule',
};
// Persist the per-session builder so it survives reload / session switch.
const LS_BUILDER_KEY = 'aiforge.chat.builderBySession';

// A pending human-approval gate (#1): the run is blocked until the user
// Approves/Rejects this action.
type PendingApproval = {
  id: number;          // seq echoed back to the server
  sessionId: number;   // the session that produced it — guards wrong-session resolve
  name: string;
  args: object;
  reason?: string;
  preview?: string;
};

// ── relative time helper ──────────────────────────────────────────────────────

function relTime(isoStr: string): string {
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

// Absolute date + time for the sidebar meta — "Jul 4, 3:14 PM" (today drops the
// date to just the time). Shown alongside the relative label.
function dateTimeLabel(isoStr: string): string {
  const d = new Date(isoStr);
  if (isNaN(d.getTime())) return '';
  const time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (sameDay) return time;
  const date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  return `${date}, ${time}`;
}

// ── Convert a persisted ChatMsg step (from server) to AgentStep ───────────────

function toAgentStep(raw: any): AgentStep | null {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.type === 'thought' || raw.kind === 'thought') {
    return { kind: 'thought', text: raw.text || '', role: raw.role };
  }
  if (raw.type === 'tool' || raw.kind === 'tool') {
    return { kind: 'tool', name: raw.name || '', args: raw.args || {}, result: raw.result || {}, role: raw.role };
  }
  if (raw.type === 'error' || raw.kind === 'error') {
    return { kind: 'error', text: raw.text || '' };
  }
  if (raw.type === 'changes' || raw.kind === 'changes') {
    const files = raw.files || [];
    return { kind: 'changes', files, summary: raw.summary || { files: files.length, additions: 0, deletions: 0 } };
  }
  return null;
}

// ── awaiting-reply detection (FE1) ────────────────────────────────────────────
// The agent can end a turn "awaiting" the user's reply. On the live turn that
// flag lives on liveTurn.awaiting; once persisted it must be recovered from the
// stored ChatMsg (top-level flag OR a message/awaiting step) so the affordance
// survives loadSession.
function msgAwaiting(msg: any): boolean {
  if (!msg || typeof msg !== 'object') return false;
  if (msg.awaiting_input === true || msg.awaiting === true) return true;
  const steps: any[] = Array.isArray(msg.steps) ? msg.steps : [];
  return steps.some(s => s && typeof s === 'object' &&
    (s.awaiting_input === true || s.type === 'awaiting' ||
     ((s.type === 'message' || s.kind === 'message') && s.awaiting_input === true)));
}

// ── durable plan dismissal (FE3) ──────────────────────────────────────────────
// loadSession rehydrates the "Approve & Execute" pill from the last assistant
// message's plan_ready step on every load. Remember the message ids the user
// dismissed (per session) so the pill doesn't resurrect on reload / switch / Stop.
const LS_DISMISSED_PLAN_PREFIX = 'aiforge.chat.dismissedPlan.';
function getDismissedPlans(sessionId: number): Set<number> {
  try {
    const raw = localStorage.getItem(LS_DISMISSED_PLAN_PREFIX + sessionId);
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr : []);
  } catch { return new Set(); }
}
function addDismissedPlan(sessionId: number, msgId: number): void {
  try {
    const s = getDismissedPlans(sessionId);
    s.add(msgId);
    localStorage.setItem(LS_DISMISSED_PLAN_PREFIX + sessionId, JSON.stringify([...s]));
  } catch { /* ignore */ }
}

// ── unified-diff renderer (FE4) ───────────────────────────────────────────────
// Approval/edit previews are raw unified diffs. Rendering them through MdLite
// mangles `-`/`+`/`@@` lines (treated as bullets/emphasis), so render them in a
// monospace block with +/- line coloring and preserved whitespace instead.
function DiffView({ text }: { text: string }) {
  const CAP = 400;
  const allLines = text.split('\n');
  const lines = allLines.slice(0, CAP);
  const overflow = allLines.length - lines.length;
  return (
    <pre style={{
      margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      fontFamily: 'var(--font-mono)', fontSize: 12, lineHeight: 1.45,
    }}>
      {lines.map((ln, i) => {
        let color: string | undefined;
        let background: string | undefined;
        if (/^\+\+\+/.test(ln) || /^---/.test(ln) || /^(diff |index )/.test(ln)) {
          color = 'var(--fg-3)';
        } else if (/^\+/.test(ln)) {
          color = 'var(--ok, #3fb950)'; background = 'rgba(63,185,80,0.10)';
        } else if (/^-/.test(ln)) {
          color = 'var(--err, #e5534b)'; background = 'rgba(229,83,75,0.10)';
        } else if (/^@@/.test(ln)) {
          color = '#6aa6ff';
        }
        return <div key={i} style={{ color, background, padding: '0 4px' }}>{ln || ' '}</div>;
      })}
      {overflow > 0 && (
        <div style={{ color: 'var(--fg-3)', padding: '0 4px', fontStyle: 'italic' }}>
          …(truncated, {overflow} more line{overflow === 1 ? '' : 's'})
        </div>
      )}
    </pre>
  );
}

// Context-window (re)load control: type a context size in K and reload the
// given model on the LM Studio host at that window. No preset sizes baked in
// — the operator types any value; the backend enforces its own floor/ceiling.
function CtxReload({ model, onLoaded }: { model: string; onLoaded?: () => void }) {
  const [k, setK] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const kn = Number(k);
  const valid = !!model && Number.isFinite(kn) && kn > 0;
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, marginLeft: 4 }}>
      <input
        type="number"
        className="chat-model-select"
        style={{ width: 70 }}
        value={k}
        min={1}
        placeholder="ctx K"
        disabled={loading || !model}
        title="Context window in K tokens to (re)load this model at"
        onChange={e => setK(e.target.value)}
      />
      <button
        className="ghost sm"
        disabled={loading || !valid}
        title={model ? `Reload ${model} at ${k || '?'}K context (takes a few seconds)` : 'Pick a model first'}
        onClick={async () => {
          setLoading(true);
          try {
            const res = await chatApi.reloadModel(model, Math.round(kn * 1024));
            toast.success(`Loaded at ${Math.round(res.context_length / 1024)}K context`);
            onLoaded?.();
          } catch (err: any) {
            toast.error(`Reload failed: ${err.message}`);
          } finally {
            setLoading(false);
          }
        }}
      >
        {loading ? 'Loading…' : 'Reload @ ctx'}
      </button>
    </span>
  );
}

// ── Chat component ─────────────────────────────────────────────────────────────

export default function Chat() {
  // Sessions list
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem('aiforge.chat.sidebarCollapsed') === '1'; } catch { return false; }
  });
  const toggleSidebar = () => setSidebarCollapsed(v => {
    const n = !v;
    try { localStorage.setItem('aiforge.chat.sidebarCollapsed', n ? '1' : '0'); } catch { /* ignore */ }
    return n;
  });

  // Active session
  const [activeId, setActiveId] = useState<number | null>(() => {
    try {
      const v = localStorage.getItem(LS_SESSION_KEY);
      return v ? Number(v) : null;
    } catch { return null; }
  });
  // Always-current mirror of activeId so async re-attach handlers can tell
  // whether the user has since switched sessions (closures capture a stale
  // activeId; the ref doesn't). Guards a late attach from clobbering the
  // session the user moved to.
  const activeIdRef = useRef<number | null>(activeId);
  useEffect(() => { activeIdRef.current = activeId; }, [activeId]);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [msgsLoading, setMsgsLoading] = useState(false);

  // Attached images for this session (+ whether the model can see them).
  const [media, setMedia] = useState<ChatMedia[]>([]);
  const [mediaVision, setMediaVision] = useState(false);
  const [uploadingMedia, setUploadingMedia] = useState(false);
  const mediaInputRef = useRef<HTMLInputElement | null>(null);

  async function loadMedia(id: number) {
    try {
      const r = await chatMediaList(id);
      setMedia(r.media); setMediaVision(r.vision);
    } catch { /* best-effort */ }
  }
  async function uploadMedia(files: FileList | File[] | null) {
    const list = files ? Array.from(files) : [];
    if (!list.length) return;
    // Pre-flight size guard (server cap is 25MB) — fail fast with a clear
    // message instead of spinning on a doomed multi-MB POST.
    const MAX = 25 * 1024 * 1024;
    const tooBig = list.find(f => f.size > MAX);
    if (tooBig) { toast.error(`${tooBig.name} is too large (max 25 MB)`); return; }
    // Attach works even on a brand-new chat: create the session first so the
    // file has somewhere to live (mirrors send()).
    let sid = activeId;
    if (sid === null) { sid = await createSession(); if (sid === null) return; }
    setUploadingMedia(true);
    try {
      for (const f of list) await chatMediaUpload(sid, f);
      await loadMedia(sid);
      toast.success(list.length === 1
        ? `Attached: ${list[0].name}`
        : `${list.length} files attached`);
    } catch (e: any) {
      toast.error(`Upload failed: ${e.message}`);
    } finally { setUploadingMedia(false); }
  }

  // Paste an image straight into the composer (Cmd/Ctrl+V). Clipboard images
  // arrive with a generic/blank name, so give each a real, distinct filename
  // (pasted-<timestamp>.<ext>) — shown in the attachment strip.
  function onPasteMedia(e: React.ClipboardEvent) {
    const items = e.clipboardData?.items;
    if (!items) return;
    const imgs: File[] = [];
    let pi = 0;
    for (const it of Array.from(items)) {
      if (it.kind === 'file' && it.type.startsWith('image/')) {
        const f = it.getAsFile();
        if (!f) continue;
        const hasName = f.name && f.name.toLowerCase() !== 'image.png';
        if (hasName) { imgs.push(f); continue; }
        const ext = (f.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
        // Date.now() (ms) + a per-paste index so multiple images in one paste —
        // or two pastes within the same second — don't collide on one filename.
        imgs.push(new File([f], `pasted-${Date.now()}-${pi++}.${ext}`, { type: f.type }));
      }
    }
    if (imgs.length) { e.preventDefault(); uploadMedia(imgs); }
  }

  // Live turn (the assistant response being streamed right now)
  const [liveTurn, setLiveTurn] = useState<LiveTurn | null>(null);

  // Captured-rule server truth (so pills survive reload) + active gate flags.
  const [ruleById, setRuleById] = useState<Record<string, CapturedRule>>({});
  const [ruleLoaded, setRuleLoaded] = useState(false);
  const [gateFlags, setGateFlags] = useState<GateFlags | null>(null);
  function refreshRules() {
    fetchRules(activeId != null ? { session_id: activeId } : undefined)
      .then(r => {
        const m: Record<string, CapturedRule> = {};
        (r.items || []).forEach(it => { m[it.id] = it; });
        setRuleById(m);
        setRuleLoaded(true);
      })
      .catch(() => setRuleLoaded(true));
    ruleFlags().then(setGateFlags).catch(() => {});
  }
  useEffect(() => { refreshRules(); /* eslint-disable-next-line */ }, [activeId]);
  const ruleState: RuleState = {
    byId: ruleById, loaded: ruleLoaded, sessionId: activeId,
    flags: gateFlags, refresh: refreshRules,
  };

  // Composer
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  // Mirror busy into a ref so callbacks that run as .then() continuations (e.g.
  // attachToRun after selectSession) see the CURRENT value, not the stale one
  // captured at the render where the continuation was created.
  const busyRef = useRef(false);
  useEffect(() => { busyRef.current = busy; }, [busy]);
  // Guards the Steer POST against double-fire (FE6).
  const [steering, setSteering] = useState(false);
  // Mode the CURRENTLY-running turn was launched with (not the live selector,
  // which the user can flip mid-run). Team runs can't be steered — the server
  // rejects it — so the composer disables the Steer affordance for them.
  const [activeRunMode, setActiveRunMode] = useState<ChatMode>('simple');

  // Per-session action+response trace modal (reviews ~/.aiforge/chat_traces).
  const [traceTurns, setTraceTurns] = useState<import('../api').ChatTraceTurn[] | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  async function openTrace(id: number) {
    setTraceTurns(null);
    setTraceLoading(true);
    try {
      const r = await chatApi.sessionTrace(id);
      setTraceTurns(r.turns);
    } catch (e: any) {
      toast.error(`Couldn't load trace: ${e.message}`);
      setTraceTurns([]);
    } finally {
      setTraceLoading(false);
    }
  }

  // Force-full-pipeline toggle (team mode): disable the triage 'trivial'
  // fast-path so every agent runs. Persisted server-side.
  const [fullPipeline, setFullPipeline] = useState(false);
  useEffect(() => {
    api.getForceFullPipeline().then(r => setFullPipeline(!!r.enabled)).catch(() => {});
  }, []);
  async function toggleFullPipeline(next: boolean) {
    setFullPipeline(next);
    try { await api.setForceFullPipeline(next); }
    catch { setFullPipeline(!next); }   // revert on failure
  }

  // Rename state: { id, value }
  const [renaming, setRenaming] = useState<{ id: number; value: string } | null>(null);

  // Chat mode: 'simple' (single agent) | 'team' (full ADK pipeline)
  const [chatMode, setChatMode] = useState<ChatMode>(() => {
    try {
      const v = localStorage.getItem(LS_MODE_KEY);
      return (v === 'team' || v === 'plan' ? v : 'simple') as ChatMode;
    } catch { return 'simple'; }
  });

  // Per-session builder flow (job/skill/workflow/rule). When set for the active
  // session, every message that session sends carries `builder` so the backend
  // runs the interview charter instead of the enhancer/team pipeline.
  const [searchParams, setSearchParams] = useSearchParams();
  const [builderBySession, setBuilderBySession] = useState<Record<number, BuilderKind>>(() => {
    try { return JSON.parse(localStorage.getItem(LS_BUILDER_KEY) || '{}'); } catch { return {}; }
  });
  useEffect(() => {
    try { localStorage.setItem(LS_BUILDER_KEY, JSON.stringify(builderBySession)); } catch { /* ignore */ }
  }, [builderBySession]);
  function setBuilderForSession(id: number, kind: BuilderKind) {
    setBuilderBySession(prev => ({ ...prev, [id]: kind }));
  }

  // Launch a builder from a `?builder=<kind>` query param: create a fresh session
  // in that builder mode, then clear the param so a reload doesn't spawn another.
  const builderLaunchedRef = useRef(false);
  useEffect(() => {
    const b = searchParams.get('builder');
    if (!b) return;
    if (!BUILDER_KINDS.includes(b as BuilderKind)) {
      setSearchParams({}, { replace: true });
      return;
    }
    if (builderLaunchedRef.current) return;
    builderLaunchedRef.current = true;
    (async () => {
      const id = await createSession();
      if (id !== null) setBuilderForSession(id, b as BuilderKind);
      setSearchParams({}, { replace: true });
      setTimeout(() => textareaRef.current?.focus(), 50);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Pre-apply "Review edits" mode (Gap D) is FORCED ON for every chat window —
  // no UI toggle. Every file-mutating tool call is held for Approve/Reject
  // (with a diff) before it lands in simple/plan mode. (Team/parallel runs
  // can't hold edits; the server surfaces a one-time notice there.)
  const reviewEdits = true;
  // Cave mode (lean context) is now AUTO-enabled for small model windows
  // (≤48K) server-side, so the per-chat toggle was removed — it's the
  // default for local models. Advanced operators can still force it on/off
  // from the Settings LLM card.

  // Pending approval gate (#1) + checkpoints panel (#3).
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  // Plan→approve→execute (Gap B): set when a plan-mode run emits a plan_ready
  // event carrying the approved spec the user can one-click execute as a team run.
  const [planReady, setPlanReady] = useState<{ spec: string; msgId?: number } | null>(null);
  const [checkpoints, setCheckpoints] = useState<Array<{ sha: string; label: string; when: string }> | null>(null);
  // Edit-and-resend: the user-message id whose turn we're replacing (history is
  // truncated there + workspace restored to that turn's checkpoint on send).
  const [editingFrom, setEditingFrom] = useState<number | null>(null);

  // Model selector
  const [modelOptions, setModelOptions] = useState<ChatModelEntry[]>([]);
  const [chatProvider, setChatProvider] = useState<string>('');
  const [selectedModel, setSelectedModel] = useState<string>(() => {
    try { return localStorage.getItem(LS_MODEL_KEY) || ''; } catch { return ''; }
  });
  const [modelActive, setModelActive] = useState<boolean>(true);
  // Orchestrator model (enhancer + planner — the layer-1 splitter agents)
  const [orchModel, setOrchModel] = useState<string>('');
  const [orchOptions, setOrchOptions] = useState<{ id: string; label: string }[]>([]);

  // Elapsed timer for the live streaming turn
  const [elapsedSec, setElapsedSec] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const sendStartRef = useRef<number>(0);

  const logRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const renameInputRef = useRef<HTMLInputElement | null>(null);
  // Header overflow menu (secondary session actions).
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [menuOpen]);
  // Aborts the in-flight chat stream (Stop button + cleanup on
  // unmount / session switch so a half-streamed turn doesn't leak).
  const abortRef = useRef<AbortController | null>(null);

  function stopRun() {
    // Tell the server to halt the run (agents + sub-agents + subprocesses)
    // FIRST, then drop the client stream.
    if (activeId !== null) chatSessionStop(activeId);
    abortRef.current?.abort();
    abortRef.current = null;
    if (timerRef.current !== null) { clearInterval(timerRef.current); timerRef.current = null; }
    setBusy(false);
    setPendingApproval(null);   // don't leave a resolvable card on a killed run
    // Optimistically mark any still-running/pending subtasks as failed so the
    // panel doesn't show perpetually-running rows before the reload settles.
    const TERMINAL = new Set(['done', 'failed', 'skipped', 'won']);
    setLiveTurn(prev => prev ? {
      ...prev,
      streaming: false,
      subtasks: prev.subtasks?.map(s =>
        TERMINAL.has(s.status) ? s : { ...s, status: 'failed' }),
    } : null);
    toast('Stopping run…');
    // The run keeps unwinding server-side (cancellation is cooperative — the
    // current LLM call must return first), and the producer persists in its
    // finally. Re-attach instead of a guessed 800ms reload: the attach tails
    // the run to its real `done` then reconciles to the persisted turn, so the
    // stopped partial is never wiped by reloading before persistence lands.
    if (activeId !== null) {
      const sid = activeId;
      setTimeout(() => attachToRun(sid), 150);
    }
  }

  // ── Kill all (force reset) ────────────────────────────────────────────────
  // Escape hatch for a wedged run: cancels every run server-side, clears the
  // gates, and force-releases the team run lock — so a new chat never sits on
  // "waiting for another team run to finish". Also clears local run state.
  async function killAll() {
    abortRef.current?.abort();
    abortRef.current = null;
    if (timerRef.current !== null) { clearInterval(timerRef.current); timerRef.current = null; }
    setBusy(false);
    setLiveTurn(prev => prev ? { ...prev, streaming: false } : null);
    setPendingApproval(null);
    try {
      const r = await chatKillAll();
      toast.success(`Reset — ${r.count} run${r.count === 1 ? '' : 's'} stopped${r.team_lock_released ? ', team lock released' : ''}`);
    } catch (e: any) {
      toast.error(`Reset failed: ${e.message}`);
    }
    if (activeId !== null) setTimeout(() => loadSession(activeId), 600);
  }

  // ── Approval gate (#1) ──────────────────────────────────────────────────────
  async function resolveApproval(decision: 'approve' | 'reject') {
    const p = pendingApproval;
    setPendingApproval(null);   // optimistic — run resumes server-side
    if (!p || activeId === null) return;
    if (p.sessionId !== activeId) return;   // stale card from another session
    try {
      await chatApi.approve(p.sessionId, decision, p.id);
    } catch (e: any) {
      toast.error(`Approval failed: ${e.message}`);
    }
  }

  // ── Checkpoints (#3) ────────────────────────────────────────────────────────
  async function openCheckpoints() {
    if (activeId === null) return;
    try {
      const res = await chatApi.checkpoints(activeId);
      setCheckpoints(res.checkpoints || []);
    } catch (e: any) {
      toast.error(`Couldn't load checkpoints: ${e.message}`);
    }
  }

  async function restoreCheckpoint(sha: string, deleteOrphans = false) {
    if (activeId === null) return;
    const msg = deleteOrphans
      ? 'FULL restore to this checkpoint? The tree is made to EXACTLY match the snapshot — files created after it are DELETED. This cannot be undone.'
      : 'Restore the workspace to this checkpoint? Tracked files revert to the snapshot; files created after it are left in place.';
    if (!window.confirm(msg)) return;
    try {
      const res = await chatApi.checkpointRestore(activeId, sha, { delete_orphans: deleteOrphans });
      if (res.ok) {
        const nLeft = res.left_in_place?.length || 0;
        const nDel = res.deleted?.length || 0;
        toast.success('Restored'
          + (nDel ? ` (${nDel} newer file(s) deleted)` : '')
          + (nLeft ? ` (${nLeft} newer file(s) left in place)` : ''));
        setCheckpoints(null);
      } else {
        toast.error(`Restore failed: ${res.error || 'unknown'}`);
      }
    } catch (e: any) {
      toast.error(`Restore failed: ${e.message}`);
    }
  }

  // ── Load sessions list ─────────────────────────────────────────────────────

  async function loadSessions(silent = false) {
    if (!silent) setSessionsLoading(true);
    try {
      const list = await chatApi.sessions();
      setSessions(list);
    } catch (e: any) {
      if (!silent) toast.error(`Failed to load sessions: ${e.message}`);
    } finally {
      if (!silent) setSessionsLoading(false);
    }
  }

  // ── Load a specific session's messages ────────────────────────────────────

  async function loadSession(id: number) {
    setMsgsLoading(true);
    setMessages([]);
    setLiveTurn(null);
    setMedia([]);               // reset attachments while the session loads
    setPendingApproval(null);   // don't carry a card across sessions/loads
    setPlanReady(null);         // don't let session A's plan execute in session B
    setCheckpoints(null);
    loadMedia(id);              // pull this session's attached images
    try {
      const res = await chatApi.sessionGet(id);
      setMessages(res.messages);
      // M4 — rehydrate the "Approve & Execute" button if the LAST assistant
      // turn ended with an un-acted plan_ready step (persisted server-side but
      // dropped by toAgentStep). Only the last turn — older plans are stale.
      try {
        const msgs = res.messages || [];
        const lastAssistant = [...msgs].reverse().find((m: any) => m.role === 'assistant');
        const isLastTurn = msgs.length > 0 && msgs[msgs.length - 1] === lastAssistant;
        if (lastAssistant && isLastTurn) {
          const pr = (lastAssistant.steps || []).find((s: any) => s?.type === 'plan_ready');
          // FE3: skip rehydration if the user already dismissed THIS plan.
          if (pr && !getDismissedPlans(id).has(lastAssistant.id)) {
            setPlanReady({ spec: pr.spec || '', msgId: lastAssistant.id });
          }
        }
      } catch { /* best-effort rehydrate — ignore */ }
    } catch (e: any) {
      toast.error(`Failed to load session: ${e.message}`);
      // Session may have been deleted; clear active
      setActiveId(null);
      try { localStorage.removeItem(LS_SESSION_KEY); } catch { /* ignore */ }
    } finally {
      setMsgsLoading(false);
    }
  }

  // ── On mount: load sessions + models; if active ID persisted, load it too ──

  useEffect(() => {
    // Fetch available chat model options (non-blocking; failures are silent)
    chatApi.chatModels().then(async resp => {
      setChatProvider(resp.provider || '');
      const models = resp.models || [];
      setModelOptions(models);

      // If saved selection is not active AND there is at least one active model, auto-switch
      if (resp.current_active === false && models.length > 0) {
        const firstActive = models[0];
        setSelectedModel(firstActive.id);
        setModelActive(firstActive.active);
        try {
          await chatApi.setChatModel(firstActive.id, resp.provider || undefined);
        } catch { /* ignore — best-effort */ }
        toast.warning(`Previous chat model not loaded — switched to ${firstActive.label || firstActive.id}`);
      } else {
        // Use backend's current as ground truth; fall back to localStorage, then first option
        setSelectedModel(prev => {
          const backendCurrent = resp.current || '';
          const localPersisted = prev;
          const allIds = models.map((m: ChatModelEntry) => m.id);
          if (backendCurrent && allIds.includes(backendCurrent)) return backendCurrent;
          if (localPersisted && allIds.includes(localPersisted)) return localPersisted;
          return backendCurrent || (models[0]?.id ?? '');
        });
        setModelActive(resp.current_active ?? true);
      }
    }).catch(() => { /* backend may not have the endpoint yet — ignore */ });

    chatApi.orchestratorModel().then(r => {
      setOrchModel(r.model || '');
      setOrchOptions(r.models || []);
    }).catch(() => { /* endpoint optional — ignore */ });

    loadSessions().then(() => {
      // sessions loaded; if there's an activeId, load it — then re-attach to
      // any run still in flight server-side (navigated away mid-run + back).
      if (activeId !== null) {
        loadSession(activeId).then(() => attachToRun(activeId));
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Persist active session id
  useEffect(() => {
    try {
      if (activeId !== null) {
        localStorage.setItem(LS_SESSION_KEY, String(activeId));
      } else {
        localStorage.removeItem(LS_SESSION_KEY);
      }
    } catch { /* ignore */ }
  }, [activeId]);

  // Persist selected model
  useEffect(() => {
    try { if (selectedModel) localStorage.setItem(LS_MODEL_KEY, selectedModel); } catch { /* ignore */ }
  }, [selectedModel]);

  // Persist chat mode
  useEffect(() => {
    try { localStorage.setItem(LS_MODE_KEY, chatMode); } catch { /* ignore */ }
  }, [chatMode]);

  // Auto-scroll on new messages / live turn updates
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, liveTurn]);

  // Focus rename input when rename starts
  useEffect(() => {
    if (renaming) {
      setTimeout(() => renameInputRef.current?.focus(), 30);
    }
  }, [renaming]);

  // Cleanup timer interval on unmount to prevent leaks
  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  // Abort the in-flight stream when leaving the Chat view (navigate away).
  useEffect(() => () => { abortRef.current?.abort(); }, []);

  // ── Select a session ──────────────────────────────────────────────────────

  function selectSession(id: number) {
    if (id === activeId) return;
    abortRef.current?.abort();   // drop any in-flight stream on the old session
    abortRef.current = null;
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    setActiveId(id);
    activeIdRef.current = id;     // sync immediately so attachToRun's guard is right
    setLiveTurn(null);
    setBusy(false);
    // Load history, then re-attach to any run still in flight for this session
    // so switching back to it resumes the live progress instead of losing it.
    loadSession(id).then(() => attachToRun(id));
  }

  // ── Create a new session ──────────────────────────────────────────────────

  async function createSession(cwd?: string): Promise<number | null> {
    try {
      const session = await chatApi.sessionCreate(cwd ? { cwd } : undefined);
      setSessions(prev => [session, ...prev]);
      abortRef.current?.abort();   // drop any stream still tied to the old session
      abortRef.current = null;
      setActiveId(session.id);
      activeIdRef.current = session.id;
      setMessages([]);
      setLiveTurn(null);
      setMedia([]);                // fresh chat → no attachments yet
      setBusy(false);              // a fresh chat is never mid-run
      setPendingApproval(null);
      return session.id;
    } catch (e: any) {
      toast.error(`Failed to create session: ${e.message}`);
      return null;
    }
  }

  async function handleNewChat() {
    await createSession();
    setTimeout(() => textareaRef.current?.focus(), 50);
  }

  // M1: new chat with an explicit working directory (the agent reads/writes
  // there). Blank = default isolated workspace.
  async function handleNewChatHere() {
    const dir = window.prompt('Working directory for this chat (absolute path; blank = default workspace):', '');
    if (dir === null) return;   // cancelled
    await createSession(dir.trim() || undefined);
    setTimeout(() => textareaRef.current?.focus(), 50);
  }

  // ── Delete a session ──────────────────────────────────────────────────────

  async function deleteSession(id: number) {
    if (!window.confirm('Delete this conversation?')) return;
    // Stop any run still in flight for this session before deleting it —
    // otherwise the background producer keeps running and persists against a
    // deleted session id. Stop server-side + drop our stream if it's active.
    chatSessionStop(id);
    if (activeId === id) { abortRef.current?.abort(); abortRef.current = null; }
    try {
      await chatApi.sessionDelete(id);
      setSessions(prev => prev.filter(s => s.id !== id));
      if (activeId === id) {
        setActiveId(null);
        activeIdRef.current = null;
        setMessages([]);
        setLiveTurn(null);
        setBusy(false);
      }
    } catch (e: any) {
      toast.error(`Delete failed: ${e.message}`);
    }
  }

  // ── Rename a session ──────────────────────────────────────────────────────

  function startRename(session: ChatSession, e: React.MouseEvent) {
    e.stopPropagation();
    setRenaming({ id: session.id, value: session.title });
  }

  async function commitRename() {
    if (!renaming) return;
    const trimmed = renaming.value.trim();
    setRenaming(null);
    if (!trimmed) return;
    // Find current title to check if changed
    const current = sessions.find(s => s.id === renaming.id);
    if (current && current.title === trimmed) return;
    try {
      const updated = await chatApi.sessionRename(renaming.id, trimmed);
      setSessions(prev => prev.map(s => s.id === renaming.id ? { ...s, title: updated.title } : s));
    } catch (e: any) {
      toast.error(`Rename failed: ${e.message}`);
    }
  }

  function onRenameKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') { e.preventDefault(); commitRename(); }
    if (e.key === 'Escape') { setRenaming(null); }
  }

  // ── SSE stream pump (shared by send + reattach) ───────────────────────────
  // Reads the SSE body to completion, applying each event to the live turn.
  // Used both by a fresh `send` and by `attachToRun` (resume after navigating
  // away and back) — so a re-attached run renders identically to a live one.
  async function pumpStream(res: Response, sessionId: number): Promise<void> {
    const reader = res.body!.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    function applyEvent(raw: string) {
      const line = raw.startsWith('data: ') ? raw.slice(6) : raw;
      if (!line.trim()) return;
      let evt: any;
      try { evt = JSON.parse(line); } catch { return; }

      // Heartbeat — keeps the SSE connection warm on a slow model. No-op.
      if (evt.type === 'ping') return;

      // Re-attach handshake (first event from /attach): if a run is live,
      // bring the view back into the streaming state — busy spinner, a live
      // turn for the replayed events to populate, and the elapsed timer seeded
      // from the run's TRUE start so the duration is continuous (not reset).
      if (evt.type === 'attached') {
        if (evt.running) {
          setBusy(true);
          setLiveTurn(prev => prev ?? { role: 'assistant', text: '', steps: [], streaming: true });
          sendStartRef.current = evt.started_at ? evt.started_at * 1000 : Date.now();
          setElapsedSec(Math.max(0, Math.floor((Date.now() - sendStartRef.current) / 1000)));
          if (timerRef.current !== null) clearInterval(timerRef.current);
          timerRef.current = setInterval(() => {
            setElapsedSec(Math.floor((Date.now() - sendStartRef.current) / 1000));
          }, 1000);
        }
        return;
      }

      // Approval gate (#1): the run is blocked server-side; surface the
      // action + diff so the user can Approve/Reject. Cleared when the
      // next tool/message event arrives (the run resumed).
      if (evt.type === 'approval') {
        setPendingApproval({ id: evt.id, sessionId, name: evt.name, args: evt.args || {}, reason: evt.reason, preview: evt.preview });
        return;
      }
      if (evt.type === 'tool' || evt.type === 'message') setPendingApproval(null);

      // M4: a gate that timed out while the user was away — clear the card and
      // leave an inline note so it's not a silent auto-reject.
      if (evt.type === 'approval_expired') {
        setPendingApproval(null);
        setLiveTurn(prev => prev ? { ...prev, steps: [...prev.steps,
          { kind: 'thought' as const, role: 'system',
            text: `⏲ approval for ${evt.name} expired — action was not run` }] } : prev);
        return;
      }

      // M5: an auto-approved (captured-flag) action — render the audit pill
      // the backend emits for attributability (was dropped before).
      if (evt.type === 'auto_approved') {
        setLiveTurn(prev => prev ? { ...prev, steps: [...prev.steps,
          { kind: 'thought' as const, role: 'system',
            text: `⚡ auto-approved ${evt.name} (flag: ${evt.flag}${evt.scope ? ` · ${evt.scope}` : ''})` }] } : prev);
        return;
      }

      // M3: context-window usage — keep the latest on the live turn for the
      // footer meter.
      if (evt.type === 'usage') {
        setLiveTurn(prev => prev ? { ...prev, usage: { pct: evt.pct, chars: evt.context_chars, budget: evt.budget_chars, tokens: evt.context_tokens, windowTokens: evt.window_tokens } } : prev);
        return;
      }

      // Plan ready (Gap B): a plan-mode run produced an approvable spec.
      if (evt.type === 'plan_ready') {
        setPlanReady({ spec: evt.spec || '' });
        return;
      }

      // Rule/Memory/Feedback captured (deterministic capture pass): render an
      // inline pill (change-scope / undo). Append to the live turn.
      if (evt.type === 'captured') {
        setLiveTurn(prev => prev ? {
          ...prev,
          captured: [...(prev.captured || []), {
            id: evt.id, category: evt.category, scope: evt.scope,
            text: evt.text || '', repo: evt.repo,
            gate_intent: evt.gate_intent,
          }],
        } : prev);
        return;
      }

      setLiveTurn(prev => {
        if (!prev) return prev;
        if (evt.type === 'subtasks') {
          return { ...prev, subtasks: evt.items || [] };
        }
        if (evt.type === 'subtask_update' && prev.subtasks) {
          return { ...prev, subtasks: prev.subtasks.map(s =>
            s.slug === evt.slug ? { ...s, status: evt.status } : s) };
        }
        if (evt.type === 'thought') {
          return { ...prev, steps: [...prev.steps, { kind: 'thought' as const, text: evt.text, role: evt.role }] };
        }
        if (evt.type === 'tool') {
          return { ...prev, steps: [...prev.steps, { kind: 'tool' as const, name: evt.name, args: evt.args || {}, result: evt.result || {}, role: evt.role }] };
        }
        if (evt.type === 'changes') {
          return { ...prev, steps: [...prev.steps, { kind: 'changes' as const, files: evt.files || [], summary: evt.summary || { files: (evt.files || []).length, additions: 0, deletions: 0 } }] };
        }
        if (evt.type === 'message') {
          if (evt.awaiting_input) setTimeout(() => textareaRef.current?.focus(), 30);
          return { ...prev, text: evt.text, streaming: false, awaiting: !!evt.awaiting_input };
        }
        if (evt.type === 'error') {
          return { ...prev, text: evt.text, steps: [...prev.steps, { kind: 'error' as const, text: evt.text }], streaming: false };
        }
        if (evt.type === 'done') {
          return { ...prev, streaming: false };
        }
        return prev;
      });
    }

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() ?? '';
      for (const part of parts) {
        if (part.trim()) {
          for (const line of part.split('\n')) {
            if (line.startsWith('data: ')) applyEvent(line);
          }
        }
      }
    }

    // Flush remaining buffer
    if (buffer.trim()) {
      for (const line of buffer.split('\n')) {
        if (line.startsWith('data: ')) applyEvent(line);
      }
    }
  }

  // ── Re-attach to a run still in flight on the server ──────────────────────
  // When the user navigates away from Chat and returns (or reloads), the run
  // keeps executing server-side (it's on a background thread now). This probes
  // for a live run and, if found, resumes streaming its progress so nothing is
  // lost. No-op when nothing is in flight.
  async function attachToRun(sessionId: number) {
    if (busyRef.current) return;   // our own send is already streaming this session
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await fetch(chatSessionAttachURL(sessionId), { signal: ctrl.signal });
      if (!res.ok || !res.body) return;
      await pumpStream(res, sessionId);
      // If a run was live, the pump set busy/liveTurn via the `attached`
      // handler. Reconcile to the now-persisted turn (mirrors send()'s tail).
      // Only when WE are still the active stream (a send() that superseded this
      // attach owns abortRef now) AND still on this session — otherwise we'd
      // stomp the newer run or a switched-to session.
      if (abortRef.current === ctrl && activeIdRef.current === sessionId) {
        if (timerRef.current !== null) { clearInterval(timerRef.current); timerRef.current = null; }
        await loadSession(sessionId);
        setLiveTurn(null);
        loadSessions(true);
      }
    } catch (e: any) {
      // Abort (navigated away / session switch / superseded by send) is
      // expected — not an error.
      if (e?.name !== 'AbortError') { /* attach is best-effort; ignore */ }
    } finally {
      // Only clear shared state if this attach is still the active stream — a
      // send() that took over must not have its busy/abortRef cleared by us.
      if (abortRef.current === ctrl) {
        abortRef.current = null;
        if (activeIdRef.current === sessionId) setBusy(false);
      }
    }
  }

  // ── SSE streaming send ────────────────────────────────────────────────────

  async function send(overrideContent?: string, overrideMode?: ChatMode) {
    const q = (overrideContent ?? input).trim();
    if (!q || busy) return;
    // Abort any in-flight attach probe on this session first — an unresolved
    // attach (kicked off by mount/selectSession) would otherwise keep its fetch
    // alive and, on resolve, run its finally (setBusy(false) + loadSession +
    // clear liveTurn) and stomp this fresh send mid-stream.
    abortRef.current?.abort();
    abortRef.current = null;
    // A fresh run supersedes any pending plan-approval (Gap B).
    setPlanReady(null);
    if (overrideContent === undefined) setInput('');
    const runMode: ChatMode = overrideMode ?? chatMode;
    setActiveRunMode(runMode);   // remember it so canSteer can disable for team
    // Edit-and-resend: consume the pending "editing from" marker for this send.
    const editFrom = editingFrom;
    setEditingFrom(null);
    // When replacing an earlier turn, drop the now-stale messages locally so the
    // optimistic append doesn't show duplicates before the server truncation.
    if (editFrom != null) {
      setMessages(prev => {
        const idx = prev.findIndex(m => m.id === editFrom);
        return idx >= 0 ? prev.slice(0, idx) : prev;
      });
    }

    // Ensure we have a session
    let sessionId = activeId;
    if (sessionId === null) {
      const newId = await createSession();
      if (newId === null) return;
      sessionId = newId;
    }

    // Optimistically append user message
    const optimisticUser: ChatMsg = {
      id: -(Date.now()), // placeholder, negative to distinguish
      role: 'user',
      content: q,
      steps: [],
      created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, optimisticUser]);

    // Start live assistant turn
    const initialLive: LiveTurn = { role: 'assistant', text: '', steps: [], streaming: true };
    setLiveTurn(initialLive);
    setBusy(true);

    // Start elapsed timer
    sendStartRef.current = Date.now();
    setElapsedSec(0);
    if (timerRef.current !== null) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - sendStartRef.current) / 1000));
    }, 1000);

    const isFirstMessage = messages.length === 0;

    // Builder flow for this session (if any): sent on EVERY message; the backend
    // ignores team/plan and forces a single-agent interview when `builder` is set.
    const builder = sessionId != null ? builderBySession[sessionId] : undefined;

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await fetch(chatSessionMessageURL(sessionId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: q, mode: builder ? 'simple' : runMode, review_edits: reviewEdits,
                               ...(builder ? { builder } : {}),
                               ...(editFrom != null ? { edit_from_message_id: editFrom } : {}) }),
        signal: ctrl.signal,
      });

      if (!res.ok) {
        let detail = '';
        try { const b = await res.json(); detail = b?.detail || b?.error || ''; } catch { /* ignore */ }
        try { if (!detail) detail = await res.text(); } catch { /* ignore */ }
        throw new Error(`${res.status} ${res.statusText}${detail ? ` — ${detail}` : ''}`);
      }

      await pumpStream(res, sessionId);

      // Ensure streaming is cleared; freeze the elapsed timer
      if (timerRef.current !== null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      const finalElapsed = Math.floor((Date.now() - sendStartRef.current) / 1000);
      setElapsedSec(finalElapsed);
      setLiveTurn(prev => prev ? { ...prev, streaming: false, elapsedSec: finalElapsed } : null);

      // Re-fetch session messages to get persisted IDs and auto-title
      await loadSession(sessionId);
      setLiveTurn(null);

      // Refresh sidebar (for auto-title on first message, and updated_at)
      if (isFirstMessage) {
        await loadSessions(true);
        // The model-generated title lands from a concurrent thread; refresh
        // again shortly to catch it if the run finished before titling did.
        setTimeout(() => loadSessions(true), 3000);
      } else {
        // Silently update the session's updated_at in the list
        loadSessions(true);
      }

    } catch (e: any) {
      if (timerRef.current !== null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      // User pressed Stop (or navigated away) — not an error.
      if (e?.name === 'AbortError') {
        setLiveTurn(prev => prev ? { ...prev, streaming: false } : null);
      } else {
        const finalElapsed = Math.floor((Date.now() - sendStartRef.current) / 1000);
        setElapsedSec(finalElapsed);
        // Render a PERSISTENT error turn — even when the failure happened before
        // any stream event created a live turn (e.g. a non-ok POST). Previously
        // `prev ? … : null` left nothing on screen, so the error only flashed in
        // a toast and vanished ("UI shows error and disappears"), forcing the
        // user to dig server logs to see what actually failed.
        const errText = `Agent error: ${e.message}`;
        setLiveTurn(prev => prev
          ? { ...prev, text: errText, streaming: false, elapsedSec: finalElapsed }
          : { role: 'assistant', text: errText, steps: [], streaming: false, elapsedSec: finalElapsed });
        toast.error(`Agent failed: ${e.message}`);
      }
    } finally {
      abortRef.current = null;
      setBusy(false);
      textareaRef.current?.focus();
    }

  }

  // ── Mid-run steering (Gap A) ───────────────────────────────────────────────
  // While a run is streaming, the Enter/Send action injects guidance into the
  // LIVE run (queued + folded in at the agent's next step) instead of opening
  // a new turn. The server echoes a role:'steer' thought when it's applied.
  async function steer() {
    const q = input.trim();
    // FE2: never steer a gated run (resolve the approval first).
    // FE6: ignore re-entry while a steer POST is already in flight.
    if (!q || !busy || activeId === null || pendingApproval || steering) return;
    setSteering(true);
    setInput('');
    try {
      const r = await chatSessionSteer(activeId, q);
      if (r.queued) {
        // FE5: rely on the server's role:'steer' echo instead of an
        // optimistic note that's never reconciled if the run ends first.
        toast('Steer queued — applies at the next step');
      } else if (r.unsupported) {
        setInput(q);   // restore — nothing was queued
        toast('Steering not available in team mode');
      } else {
        setInput(q);   // restore so the user can retry or Stop
        toast('Could not steer (the run may have ended)');
      }
    } finally {
      setSteering(false);
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  const activeSession = sessions.find(s => s.id === activeId) || null;
  const activeBuilder = activeId !== null ? builderBySession[activeId] : undefined;

  // Composer state machine (FE1/FE2): `busy` conflates three states. Steering is
  // only valid while ACTUALLY running — not while a turn is awaiting the user's
  // reply, and not while an approval gate is open.
  const lastAssistantMsg = [...messages].reverse().find(m => m.role === 'assistant') || null;
  const lastUserMsg = [...messages].reverse().find(m => m.role === 'user') || null;
  // M2: re-run the last request (re-asks; appends a fresh turn).
  function regenerate() {
    if (busy || !lastUserMsg?.content) return;
    send(lastUserMsg.content);
  }
  // M2: pull the last user message back into the composer to edit + resend.
  function editLastUser() {
    if (busy || !lastUserMsg?.content) return;
    editUserMessage(lastUserMsg);
  }
  // Edit-and-resend ANY earlier user turn: pull it into the composer and mark
  // the turn so sending truncates history there (server also restores the
  // workspace to that turn's checkpoint) before re-running.
  function editUserMessage(msg: ChatMsg) {
    if (busy || !msg?.content || msg.role !== 'user') return;
    setInput(msg.content);
    setEditingFrom(msg.id > 0 ? msg.id : null);
    setTimeout(() => textareaRef.current?.focus(), 30);
  }
  // M2: copy with uniform feedback (clipboard may be absent in insecure HTTP).
  function copyText(t: string) {
    if (!navigator.clipboard) { toast.error('Copy needs HTTPS'); return; }
    navigator.clipboard.writeText(t).then(() => toast.success('Copied'),
                                          () => toast.error('Copy failed'));
  }
  const isLastTurn = messages.length > 0 && lastAssistantMsg === messages[messages.length - 1];
  const persistedAwaiting = !!(isLastTurn && lastAssistantMsg && msgAwaiting(lastAssistantMsg));
  // The current turn is waiting for the user to answer — Enter/primary button
  // must SEND a reply (a normal turn), not steer.
  const awaitingReply = !!liveTurn?.awaiting || persistedAwaiting;
  // Steering is only valid while genuinely running (not awaiting, not gated) AND
  // the running turn is steerable — a TEAM run isn't (the server rejects it), so
  // the Steer affordance is disabled for it instead of firing a doomed POST.
  // Team/parallel runs ARE steerable now — a steer is folded into SPEC.md and
  // guides the remaining subtasks + the final reconcile.
  const canSteer = busy && !awaitingReply && !pendingApproval;

  // SPEC.md preview modal (opened from the subtask dock header).
  const [specModal, setSpecModal] = useState<{ loading: boolean; content: string } | null>(null);
  async function openSpec() {
    if (activeId === null) return;
    setSpecModal({ loading: true, content: '' });
    try {
      const r = await chatSessionSpec(activeId);
      setSpecModal({ loading: false, content: r.exists ? (r.content || '') : '_No SPEC.md in this workspace yet — it’s written when a team/parallel plan runs._' });
    } catch (e: any) {
      setSpecModal({ loading: false, content: `Failed to load SPEC.md: ${e.message}` });
    }
  }

  // Subtasks for the pinned bottom dock: the live run's list (updates status
  // live), else the most recent finished turn that carried a decomposition.
  const dockSubtasks: SubtaskItem[] | undefined = (() => {
    if (liveTurn?.subtasks && liveTurn.subtasks.length) return liveTurn.subtasks;
    for (let i = messages.length - 1; i >= 0; i--) {
      const st = (messages[i].steps || []).find((s: any) => s?.type === 'subtasks');
      if (st?.items?.length) return st.items as SubtaskItem[];
    }
    return undefined;
  })();

  function onKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (awaitingReply) { send(); return; }   // FE1: reply, not steer
      if (busy) {
        if (pendingApproval) return;            // FE2: resolve the gate first
        if (canSteer) steer();                  // team runs aren't steerable
      } else {
        send();
      }
    }
  }

  return (
    <RuleStateCtx.Provider value={ruleState}>
    <div className={`chat-shell-v2${sidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
      {/* Floating expand button — only when collapsed */}
      {sidebarCollapsed && (
        <button className="chat-sidebar-expand" title="Show chats" onClick={toggleSidebar}>
          <Icon.ChevronRight size={16} />
        </button>
      )}
      {/* ── Left sidebar: sessions list ─────────────────────────────────────── */}
      <div className="chat-sessions-sidebar">
        <div className="chat-sessions-header" style={{ display: 'flex', gap: 6 }}>
          <button onClick={handleNewChat} disabled={busy} style={{ flex: 1 }}>
            <Icon.Plus size={13} /> New chat
          </button>
          <button onClick={toggleSidebar} title="Collapse panel"
                  style={{ padding: '0 8px', display: 'inline-flex', alignItems: 'center' }}>
            <Icon.ChevronLeft size={15} />
          </button>
          <button onClick={handleNewChatHere} disabled={busy}
                  title="New chat with a specific working directory"
                  style={{ padding: '0 8px', display: 'inline-flex', alignItems: 'center' }}>
            <Icon.Folder size={15} />
          </button>
          {sessions.length > 0 && (
            <button
              className="danger"
              title="Delete every chat session. Memory, skills, workflows and rules are NOT touched."
              disabled={busy}
              onClick={async () => {
                if (!window.confirm('Delete ALL chat sessions? Memory, skills, workflows and rules are kept.')) return;
                try {
                  const r = await api.resetChats();
                  toast.success(`Deleted ${r.deleted} chats`);
                  setMessages([]); setLiveTurn(null); setActiveId(null);
                  localStorage.removeItem(LS_SESSION_KEY);
                  await loadSessions();
                } catch (e: any) { toast.error(e.message); }
              }}
            >
              <Icon.Trash size={13} />
            </button>
          )}
        </div>
        <div className="chat-sessions-list">
          {sessionsLoading ? (
            <div style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {[1,2,3].map(i => (
                <div key={i} className="skeleton" style={{ height: 44, borderRadius: 8 }} />
              ))}
            </div>
          ) : sessions.length === 0 ? (
            <div style={{ padding: '16px 10px', textAlign: 'center', color: 'var(--fg-3)', fontSize: 'var(--fs-xs)' }}>
              No conversations yet
            </div>
          ) : sessions.map(s => (
            <div
              key={s.id}
              className={`chat-session-item ${s.id === activeId ? 'active' : ''}`}
              onClick={() => { if (!renaming || renaming.id !== s.id) selectSession(s.id); }}
            >
              <div className="chat-session-item-body">
                {renaming && renaming.id === s.id ? (
                  <input
                    ref={renameInputRef}
                    className="chat-session-rename-input"
                    value={renaming.value}
                    onChange={e => setRenaming(r => r ? { ...r, value: e.target.value } : r)}
                    onKeyDown={onRenameKey}
                    onBlur={commitRename}
                    onClick={e => e.stopPropagation()}
                  />
                ) : (
                  <>
                    <div className="chat-session-title" title={s.title}>{s.title || 'Untitled'}</div>
                    <div className="chat-session-meta">
                      <span title={new Date(s.updated_at).toLocaleString()}>
                        {dateTimeLabel(s.updated_at)} · {relTime(s.updated_at)}
                      </span>
                      {s.message_count != null && s.message_count > 0
                        ? ` · ${s.message_count} msg${s.message_count === 1 ? '' : 's'}`
                        : ''}
                    </div>
                  </>
                )}
              </div>
              {(!renaming || renaming.id !== s.id) && (
                <div className="chat-session-actions">
                  <button
                    title="Rename"
                    onClick={e => startRename(s, e)}
                  >
                    ✎
                  </button>
                  <button
                    title="Delete"
                    onClick={e => { e.stopPropagation(); deleteSession(s.id); }}
                  >
                    <Icon.X size={11} />
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* ── Main pane ──────────────────────────────────────────────────────────── */}
      <div className="chat-v2-main">
        {/* Topbar */}
        <div className="chat-topbar">
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: 1 }}>
            {/* Title — click to rename (no separate button). */}
            <button
              onClick={() => activeSession && setRenaming({ id: activeSession.id, value: activeSession.title })}
              disabled={!activeSession}
              title={activeSession ? 'Click to rename' : undefined}
              style={{ background: 'none', border: 'none', padding: 0, cursor: activeSession ? 'pointer' : 'default',
                       fontSize: 'var(--fs-md)', fontWeight: 600, color: 'var(--fg-1)',
                       textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
            >
              {activeSession ? (activeSession.title || 'Untitled') : 'Agent Chat'}
            </button>
            {activeSession && (
              <span className="xs muted" title={activeSession.cwd || 'default workspace'}
                    style={{ fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                <Icon.Folder size={12} style={{ verticalAlign: '-2px', marginRight: 4 }} />
                {activeSession.cwd
                  ? (activeSession.cwd.length > 48 ? '…' + activeSession.cwd.slice(-46) : activeSession.cwd)
                  : 'default workspace'}
              </span>
            )}
            {activeBuilder && (
              <span className="chip ok" title={BUILDER_HINTS[activeBuilder]}
                    style={{ alignSelf: 'flex-start', marginTop: 2, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                <Icon.Sparkles size={11} /> {BUILDER_LABELS[activeBuilder]}
              </span>
            )}
          </div>
          <div className="row" style={{ gap: 'var(--s-2)' }}>
            {/* Simple | Plan | Team mode toggle */}
            <div className="chat-mode-toggle" title="Simple: single agent · Plan: read-only, proposes a plan first · Team: full ADK planner→doer→learner pipeline">
              <button
                className={chatMode === 'simple' ? 'active' : ''}
                onClick={() => setChatMode('simple')}
                disabled={busy}
              >
                Simple
              </button>
              <button
                className={chatMode === 'plan' ? 'active' : ''}
                onClick={() => setChatMode('plan')}
                disabled={busy}
                title="Read-only: the agent inspects the repo and proposes a plan; switch to Simple/Team to execute"
              >
                Plan
              </button>
              <button
                className={chatMode === 'team' ? 'active' : ''}
                onClick={() => setChatMode('team')}
                disabled={busy}
              >
                Team (full flow)
              </button>
            </div>

            {/* Per-session action+response trace — review what the agent did. */}
            <button
              onClick={() => { if (activeId != null) openTrace(activeId); }}
              disabled={!activeSession}
              title="Review every action + response this session took (also on disk: ~/.aiforge/chat_traces)"
              style={{ whiteSpace: 'nowrap' }}
            >
              ⧉ Trace
            </button>

            {/* Model selector — less relevant in team mode, so hide it */}
            {chatMode !== 'team' && (
              <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 'var(--fs-xs)', color: 'var(--fg-2)' }}>
                <span
                  title={modelActive ? 'Model is loaded and active' : 'Model is not currently loaded'}
                  style={{
                    display: 'inline-block',
                    width: 8,
                    height: 8,
                    borderRadius: '50%',
                    background: modelActive ? 'var(--ok, #22c55e)' : 'var(--warn, #f59e0b)',
                    flexShrink: 0,
                  }}
                />
                Model
                <select
                  className="chat-model-select"
                  value={selectedModel}
                  onChange={async e => {
                    const newModel = e.target.value;
                    setSelectedModel(newModel);
                    try {
                      const res = await chatApi.setChatModel(newModel, chatProvider || undefined);
                      setModelActive(res.active);
                      if (res.active) {
                        toast.success('Model updated');
                      } else {
                        const label = modelOptions.find(o => o.id === newModel)?.label || newModel;
                        toast.warning(`${label} is not loaded — it may fail or take time to load`);
                      }
                    } catch (err: any) {
                      toast.error(`Failed to set model: ${err.message}`);
                    }
                  }}
                  disabled={busy || modelOptions.length === 0}
                  title="Chat model"
                >
                  {modelOptions.length === 0 ? (
                    <option value="" disabled>
                      no active models — load one in LM Studio / configure on Home
                    </option>
                  ) : (
                    modelOptions.map(opt => (
                      <option key={opt.id} value={opt.id}>
                        {opt.label}
                      </option>
                    ))
                  )}
                </select>
              </label>
            )}

            {/* Cave mode (lean context) is auto-on for small windows — the
                per-chat pill was removed; override lives in Settings. */}

            {/* Orchestrator model — the enhancer + planner (layer-1 splitter).
                Shown in team mode where those agents run. */}
            {chatMode === 'team' && orchOptions.length > 0 && (
              <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 'var(--fs-xs)', color: 'var(--fg-2)' }}
                     title="Model for the orchestrator (enhancer + planner) — the agents that analyze & split the task">
                Orchestrator
                <select
                  className="chat-model-select"
                  value={orchModel}
                  disabled={busy}
                  onChange={async e => {
                    const m = e.target.value;
                    setOrchModel(m);
                    try {
                      await chatApi.setOrchestratorModel(m, chatProvider || undefined);
                      toast.success('Orchestrator model updated (enhancer + planner)');
                    } catch (err: any) { toast.error(err.message); }
                  }}
                >
                  {orchOptions.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
                </select>
                <CtxReload model={orchModel} />
              </label>
            )}

            {/* Secondary actions tucked into an overflow menu — keeps the
                header calm (just mode + model + ⋯). */}
            <div ref={menuRef} style={{ position: 'relative' }}>
              <button className="ghost sm" onClick={() => setMenuOpen(o => !o)}
                      title="More" aria-label="More actions"
                      style={{ fontSize: 18, lineHeight: 1, padding: '2px 8px' }}>⋯</button>
              {menuOpen && (
                <div role="menu" style={{
                  position: 'absolute', right: 0, top: '110%', zIndex: 40, minWidth: 210,
                  background: 'var(--bg-0)', border: '1px solid var(--border-1)',
                  borderRadius: 8, padding: 6, boxShadow: '0 8px 24px rgba(0,0,0,0.18)',
                  display: 'flex', flexDirection: 'column', gap: 2,
                }}>
                  {/* Review-edits (hold every file change for Approve/Reject in
                      simple/plan) is always on — the informational menu row was
                      removed since it wasn't a toggle. */}
                  {/* Reload the chat model at a chosen context window. */}
                  {chatMode !== 'team' && selectedModel && (
                    <div className="chat-menu-item" style={{ ...menuItem, justifyContent: 'space-between' }}
                         title="Reload this model at a chosen context window (K tokens)">
                      <span>Reload model @ ctx</span>
                      <CtxReload model={selectedModel} onLoaded={() => setModelActive(true)} />
                    </div>
                  )}
                  {chatMode === 'team' && (
                    <label className="chat-menu-item" title="Run every agent instead of letting triage fast-path trivial requests."
                           style={menuItem}>
                      <input type="checkbox" checked={fullPipeline}
                             onChange={e => toggleFullPipeline(e.target.checked)} disabled={busy} />
                      Force full pipeline
                    </label>
                  )}
                  {activeSession && (
                    <button style={menuBtn} onClick={() => { setMenuOpen(false); openCheckpoints(); }}>
                      ↶ Checkpoints
                    </button>
                  )}
                  <button style={menuBtn} onClick={() => { setMenuOpen(false); killAll(); }}
                          title="Force-stop every run + release the team lock">
                    ⚠ Reset stuck runs
                  </button>
                  {activeSession && (
                    <>
                      <div style={{ height: 1, background: 'var(--border-1)', margin: '4px 0' }} />
                      <button style={{ ...menuBtn, color: 'var(--err)' }}
                              onClick={() => { setMenuOpen(false); deleteSession(activeSession.id); }}>
                        🗑 Delete conversation
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Shared hidden file input — used by the attach button in either
            composer (active session or brand-new chat). */}
        <input ref={mediaInputRef} type="file" multiple
               accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.csv,.txt,.md,.json,.log,.yaml,.yml"
               style={{ display: 'none' }}
               onChange={e => { uploadMedia(e.target.files); e.target.value = ''; }} />

        {/* Message log or empty state */}
        {activeId === null ? (
          <div className="chat-empty-state">
            <div className="empty-icon">💬</div>
            <h3>Start a conversation</h3>
            <p>Click <strong>New chat</strong> to begin, or select a past conversation from the sidebar.</p>
            <button onClick={handleNewChat}>
              <Icon.Plus size={14} /> New chat
            </button>
          </div>
        ) : msgsLoading ? (
          <div className="chat-log" style={{ justifyContent: 'center', alignItems: 'center' }}>
            <div className="typing"><span /><span /><span /></div>
          </div>
        ) : (
          <>
            <div className="chat-log" ref={logRef}>
              <AutoApprovalsPanel />
              {messages.length === 0 && !liveTurn && !busy && (
                <div className="chat-empty-state" style={{ flex: 'none' }}>
                  <div className="empty-icon">✨</div>
                  <p style={{ maxWidth: 320 }}>Send a message to get started. The agent can read and write files, run commands, and implement features.</p>
                </div>
              )}

              {/* Persisted messages */}
              {messages.map(msg => (
                <div
                  key={msg.id}
                  className={`bubble ${msg.role === 'user' ? 'user' : 'sys'}`}
                >
                  <div className="bubble-avatar">{msg.role === 'user' ? 'You' : 'AI'}</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {msg.role === 'assistant' ? (
                      <>
                        <AssistantBubble
                          text={msg.content}
                          steps={(msg.steps || []).map(toAgentStep).filter((s): s is AgentStep => s !== null)}
                          streaming={false}
                          /* subtasks render in the pinned bottom dock, not inline */
                          captured={(msg.steps || []).filter((s: any) => s?.type === 'captured').map((s: any) => ({
                            id: s.id, category: s.category, scope: s.scope, text: s.text || '',
                            repo: s.repo, gate_intent: s.gate_intent,
                          }))}
                          onRegenerate={msg === lastAssistantMsg && !busy ? regenerate : undefined}
                        />
                        {/* FE1: awaiting affordance survives loadSession — shown
                            on the last assistant turn when it ended awaiting. */}
                        {msg === lastAssistantMsg && persistedAwaiting && (
                          <div style={{
                            marginTop: 6, fontSize: 12, fontWeight: 600,
                            color: 'var(--accent, #2563eb)',
                          }}>
                            ❓ Waiting for your reply — answer below to continue.
                          </div>
                        )}
                      </>
                    ) : (
                      <div className="bubble-body">
                        <span style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</span>
                        {/* M2: edit/copy. Edit-and-resend works on ANY user turn
                            (not just the last) — sending replaces it + everything
                            after, restoring the workspace to that turn's checkpoint. */}
                        {!busy && (
                          <div className="xs muted" style={{ marginTop: 4, display: 'flex', gap: 8 }}>
                            <button className="ghost xs"
                                    title={msg === lastUserMsg
                                      ? 'Edit this message in the composer'
                                      : 'Edit & resend from here — replaces this and all later turns'}
                                    style={{ padding: '0 4px', cursor: 'pointer' }}
                                    onClick={() => editUserMessage(msg)}>✎ Edit</button>
                            <button className="ghost xs" title="Copy"
                                    style={{ padding: '0 4px', cursor: 'pointer' }}
                                    onClick={() => copyText(msg.content)}>⧉ Copy</button>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {/* Live streaming assistant turn */}
              {liveTurn && (
                <div className="bubble sys">
                  <div className="bubble-avatar">AI</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <AssistantBubble
                      text={liveTurn.text}
                      steps={liveTurn.steps}
                      streaming={liveTurn.streaming}
                      elapsedSec={liveTurn.streaming ? elapsedSec : liveTurn.elapsedSec}
                      /* subtasks render in the pinned bottom dock, not inline */
                      captured={liveTurn.captured}
                    />
                    {liveTurn.awaiting && (
                      <div style={{
                        marginTop: 6, fontSize: 12, fontWeight: 600,
                        color: 'var(--accent, #2563eb)',
                      }}>
                        ❓ Waiting for your reply — answer below to continue.
                      </div>
                    )}
                    {/* M3: context-window fill meter while streaming */}
                    {liveTurn.streaming && liveTurn.usage && (
                      <div className="xs muted" style={{ marginTop: 4, display: 'flex', alignItems: 'center', gap: 6 }}
                           title={`~${Math.round((liveTurn.usage.tokens ?? liveTurn.usage.chars / 4) / 1000)}k / ${Math.round((liveTurn.usage.windowTokens ?? liveTurn.usage.budget / 4) / 1000)}k tokens before auto-condense`}>
                        <span style={{ width: 60, height: 4, background: 'var(--bg-2,#222)', borderRadius: 2, overflow: 'hidden' }}>
                          <span style={{ display: 'block', height: '100%', width: `${liveTurn.usage.pct ?? 0}%`,
                                         background: (liveTurn.usage.pct ?? 0) > 85 ? 'var(--err,#e5534b)' : 'var(--accent,#2563eb)' }} />
                        </span>
                        context {Math.round((liveTurn.usage.tokens ?? liveTurn.usage.chars / 4) / 1000)}k / {Math.round((liveTurn.usage.windowTokens ?? liveTurn.usage.budget / 4) / 1000)}k ({liveTurn.usage.pct ?? 0}%)
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Approval gate (#1): run is paused, awaiting Approve/Reject */}
              {pendingApproval && (
                <div style={{
                  margin: '8px 0', padding: 12,
                  border: '1px solid var(--warn, #f59e0b)',
                  borderRadius: 8, background: 'var(--bg-1)',
                }}>
                  {(() => {
                    const a = (pendingApproval.args || {}) as any;
                    const path = a.path || a.file || a.filename || a.file_path || '';
                    const ext = String(path).split('.').pop()?.toLowerCase() || '';
                    const LANG: Record<string, string> = {
                      py: 'Python', java: 'Java', go: 'Go', ts: 'TypeScript',
                      tsx: 'TSX', js: 'JavaScript', rs: 'Rust', c: 'C', cpp: 'C++',
                      rb: 'Ruby', php: 'PHP', sh: 'Shell', xml: 'XML', json: 'JSON',
                      yaml: 'YAML', yml: 'YAML', md: 'Markdown', sql: 'SQL',
                    };
                    const lang = LANG[ext];
                    return (
                      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4,
                                    display: 'flex', alignItems: 'center', gap: 8,
                                    flexWrap: 'wrap' }}>
                        <span>⚠ Approval needed — <code>{pendingApproval.name}</code></span>
                        {path && <code style={{ fontSize: 12, opacity: 0.85 }}>{path}</code>}
                        {lang && <span style={{ fontSize: 11, padding: '1px 7px',
                                   border: '1px solid var(--fg-3)', borderRadius: 6,
                                   color: 'var(--fg-2)' }}>{lang}</span>}
                      </div>
                    );
                  })()}
                  {pendingApproval.reason && (
                    <div className="muted xs" style={{ marginBottom: 6 }}>{pendingApproval.reason}</div>
                  )}
                  {pendingApproval.preview && (
                    <div style={{
                      maxHeight: 260, overflow: 'auto', fontSize: 12,
                      background: 'var(--bg-2)', padding: 8, borderRadius: 6,
                      margin: '0 0 8px',
                    }}>
                      {/* FE4: raw unified diff — render monospace, NOT via MdLite. */}
                      <DiffView text={pendingApproval.preview} />
                    </div>
                  )}
                  <div style={{ display: 'flex', gap: 8 }}>
                    <button onClick={() => resolveApproval('approve')}>✓ Approve</button>
                    <button className="danger" onClick={() => resolveApproval('reject')}>✗ Reject</button>
                  </div>
                </div>
              )}
            </div>

            {/* Pinned subtask dock — the Planner decomposition stays stuck to
                the bottom (above the composer, OUTSIDE the scroll region) and
                updates status live via subtask_update events as the run works.
                Sourced from the live run, falling back to the most recent turn
                that carried subtasks so it persists after the run finishes. */}
            {dockSubtasks && dockSubtasks.length > 0 && (
              <div className="chat-subtask-dock" style={{
                borderTop: '1px solid var(--border,#2a2f3a)',
                background: 'var(--bg-0,#0d1017)',
                padding: '6px 10px', maxHeight: 180, overflowY: 'auto',
              }}>
                <SubtaskList items={dockSubtasks} onViewSpec={openSpec} />
              </div>
            )}

            {/* SPEC.md preview modal */}
            {specModal && (
              <div onClick={() => setSpecModal(null)}
                   style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
                            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
                <div onClick={(e) => e.stopPropagation()}
                     style={{ background: 'var(--bg-0,#0d1017)', border: '1px solid var(--border,#2a2f3a)',
                              borderRadius: 8, width: 'min(820px, 92vw)', maxHeight: '85vh', overflowY: 'auto',
                              padding: '18px 22px', boxShadow: '0 12px 40px rgba(0,0,0,0.5)' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                    <strong>📄 SPEC.md</strong>
                    <button className="ghost" style={{ cursor: 'pointer' }} onClick={() => setSpecModal(null)}>✕</button>
                  </div>
                  {specModal.loading ? <div className="muted">Loading…</div>
                    : <MdLite text={specModal.content} />}
                </div>
              </div>
            )}

            <MediaStrip media={media} vision={mediaVision}
                        onDescribe={async (id, d) => {
                          try { await chatMediaDescribe(id, d); if (activeId !== null) loadMedia(activeId); }
                          catch (e: any) { toast.error(`Save failed: ${e.message}`); }
                        }}
                        onDelete={async (id) => {
                          try { await chatMediaDelete(id); if (activeId !== null) loadMedia(activeId); }
                          catch (e: any) { toast.error(`Delete failed: ${e.message}`); }
                        }} />

            <div className="chat-composer">
              {editingFrom != null && (
                <div className="xs" style={{
                  marginBottom: 6, padding: '4px 8px', borderRadius: 6,
                  background: 'var(--bg-1)', border: '1px solid var(--warn,#f59e0b)',
                  display: 'flex', alignItems: 'center', gap: 8,
                }}>
                  <span>✎ Editing an earlier message — sending replaces it and every later turn, and restores the workspace to that turn.</span>
                  <button className="ghost xs" style={{ cursor: 'pointer' }}
                          onClick={() => { setEditingFrom(null); setInput(''); }}>cancel</button>
                </div>
              )}
              <div style={{ display: 'flex', gap: 6 }}>
                <textarea
                  ref={textareaRef}
                  onPaste={onPasteMedia}
                  /* Short while a run is active (you're mostly steering/watching,
                     not composing) — full height when idle. Drag the corner to
                     expand either way (resize: vertical). */
                  rows={busy ? 1 : 3}
                  placeholder={
                    pendingApproval
                      ? "Resolve the approval above first (Approve / Reject)…"
                      : awaitingReply
                        ? "The agent is waiting for your reply — type your answer, Enter to send…"
                        : busy
                          ? (activeRunMode === 'team'
                              ? "Steer the run — your note is folded into SPEC.md for the remaining tasks (Enter to send)…"
                              : "Steer the running agent — type guidance, Enter to inject (no Stop needed)…")
                          : "Ask the agent to read/write files, run commands, implement a feature…  (Enter to send, Shift+Enter for newline)"}
                  value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={onKey}
                  style={{ flex: 1, resize: 'vertical', minHeight: busy ? 34 : 64 }}
                />
                <button onClick={() => mediaInputRef.current?.click()} disabled={uploadingMedia}
                        title="Attach a file — image, PDF, Word, Excel, text — queryable all session"
                        style={{ whiteSpace: 'nowrap', display: 'inline-flex', alignItems: 'center' }}>
                  {uploadingMedia ? '…' : <Icon.Paperclip size={15} />}
                </button>
                {busy && (
                  <button onClick={stopRun} className="danger"
                          title="Stop all agents + processes for this run"
                          style={{ whiteSpace: 'nowrap' }}>
                    ■ Stop
                  </button>
                )}
                {canSteer ? (
                  <button onClick={steer} disabled={!input.trim() || steering}
                          title="Inject this guidance into the running agent without stopping it"
                          style={{ whiteSpace: 'nowrap' }}>
                    ↳ Steer
                  </button>
                ) : busy && !awaitingReply ? (
                  // Running but not steerable: either a team run (server rejects
                  // mid-run steering) or gated on an approval (FE2). Disabled
                  // either way, with a title that says which.
                  <button disabled
                          title={activeRunMode === 'team'
                            ? "Steering isn't available in team mode"
                            : 'Resolve the approval above before steering'}
                          style={{ whiteSpace: 'nowrap' }}>
                    ↳ Steer
                  </button>
                ) : (
                  // Idle, or awaiting the user's reply — primary action sends a
                  // normal turn (FE1).
                  <button onClick={() => send()} disabled={busy || !input.trim()}>
                    <Icon.Agents size={14} /> {awaitingReply ? 'Reply' : 'Run'}
                  </button>
                )}
              </div>
              {/* Plan→approve→execute (Gap B): one-click run the approved plan
                  as a team build. */}
              {planReady && !busy && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8,
                              marginTop: 8, padding: '8px 10px',
                              border: '1px solid var(--accent, #6366f1)',
                              borderRadius: 6, background: 'var(--bg-2)' }}>
                  <span className="small muted" style={{ flex: 1 }}>
                    Plan ready. Approve to execute it as a team build.
                  </span>
                  <button onClick={() => {
                            // FE3: remember the dismissal so loadSession (reload /
                            // session switch / Stop) doesn't resurrect the pill.
                            if (activeId !== null && planReady.msgId != null) {
                              addDismissedPlan(activeId, planReady.msgId);
                            }
                            setPlanReady(null);
                          }} className="ghost"
                          style={{ whiteSpace: 'nowrap' }}>Dismiss</button>
                  <button onClick={() => send(planReady.spec, 'team')}
                          title="Run the approved plan as a full team build"
                          style={{ whiteSpace: 'nowrap' }}>
                    ✓ Approve &amp; Execute
                  </button>
                </div>
              )}
            </div>
          </>
        )}

        {/* Composer shown even when no session: send will create one */}
        {activeId === null && (
          <div className="chat-composer">
            <div style={{ display: 'flex', gap: 6 }}>
              <textarea
                ref={textareaRef}
                onPaste={onPasteMedia}
                rows={4}
                placeholder="Type a message to start a new conversation…  (Enter to send, paste or attach an image too)"
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={onKey}
                disabled={busy}
                style={{ flex: 1, minHeight: 96, resize: 'vertical',
                         fontSize: 14, lineHeight: 1.5, padding: 10 }}
              />
              <button onClick={() => mediaInputRef.current?.click()} disabled={uploadingMedia}
                      title="Attach a file — image, PDF, Word, Excel, text — starts a chat, queryable all session"
                      style={{ whiteSpace: 'nowrap', display: 'inline-flex', alignItems: 'center' }}>
                {uploadingMedia ? '…' : <Icon.Paperclip size={15} />}
              </button>
              {busy && (
                <button onClick={stopRun} className="danger"
                        title="Stop all agents + processes for this run"
                        style={{ whiteSpace: 'nowrap' }}>
                  ■ Stop
                </button>
              )}
              <button onClick={() => send()} disabled={busy || !input.trim()}>
                <Icon.Agents size={14} /> Run
              </button>
            </div>
          </div>
        )}

        {/* Checkpoints panel (#3) */}
        {checkpoints !== null && (
          <div
            onClick={() => setCheckpoints(null)}
            style={{
              position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50,
            }}
          >
            <div onClick={e => e.stopPropagation()} style={{
              width: 'min(560px, 92vw)', maxHeight: '70vh', overflow: 'auto',
              background: 'var(--bg-0)', border: '1px solid var(--border-1)',
              borderRadius: 10, padding: 16,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                <strong>Workspace checkpoints</strong>
                <button className="ghost sm" onClick={() => setCheckpoints(null)}><Icon.X size={12} /></button>
              </div>
              {checkpoints.length === 0 ? (
                <div className="muted xs">No checkpoints yet. A snapshot is taken automatically before each turn (in a git repo).</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {checkpoints.map(c => (
                    <div key={c.sha} style={{
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                      gap: 8, padding: 8, border: '1px solid var(--border-0)', borderRadius: 6,
                    }}>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.label}</div>
                        <div className="muted xs" style={{ fontFamily: 'var(--font-mono)' }}>{c.when} · {c.sha.slice(0, 8)}</div>
                      </div>
                      <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                        <button className="ghost sm" onClick={() => restoreCheckpoint(c.sha)} title="Revert tracked files to this snapshot; keep files created after it">↶ Restore</button>
                        <button className="ghost sm danger" onClick={() => restoreCheckpoint(c.sha, true)} title="Full restore: make the tree exactly match this snapshot — deletes files created after it">⤓ Full</button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* ── Per-session action+response trace ──────────────────────────── */}
        {(traceLoading || traceTurns !== null) && (
          <div
            onClick={() => { setTraceTurns(null); setTraceLoading(false); }}
            style={{
              position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50,
            }}
          >
            <div onClick={e => e.stopPropagation()} style={{
              width: 'min(760px, 94vw)', maxHeight: '82vh', overflow: 'auto',
              background: 'var(--bg-0)', border: '1px solid var(--border-1)',
              borderRadius: 10, padding: 16,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <strong>Session trace — actions &amp; responses</strong>
                <button className="ghost sm" onClick={() => setTraceTurns(null)}><Icon.X size={12} /></button>
              </div>
              <div className="muted xs" style={{ marginBottom: 10 }}>
                Every turn's actions + response. Also on disk: <code>~/.aiforge/chat_traces/session_{activeId}.md</code>
              </div>
              {traceLoading ? (
                <div className="muted xs">Loading…</div>
              ) : !traceTurns || traceTurns.length === 0 ? (
                <div className="muted xs">No trace yet for this session — send a message first.</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {traceTurns.map((t, i) => (
                    <div key={i} style={{ border: '1px solid var(--border-0)', borderRadius: 8, padding: 10 }}>
                      <div className="row" style={{ gap: 6, alignItems: 'center', marginBottom: 6 }}>
                        <span className={`chip sm ${t.mode === 'team' ? 'warn' : ''}`}>{t.mode}</span>
                        <span className="muted xs" style={{ fontFamily: 'var(--font-mono)' }}>{t.ts}</span>
                        <span className="muted xs">· {t.n_tools} tool{t.n_tools === 1 ? '' : 's'}</span>
                      </div>
                      <div style={{ fontSize: 13, marginBottom: 6 }}><b>You:</b> {t.prompt}</div>
                      {t.actions && t.actions.length > 0 && (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, marginBottom: 6,
                                      paddingLeft: 8, borderLeft: '2px solid var(--border-1)' }}>
                          {t.actions.map((a, j) => {
                            const ok = a.result && typeof a.result === 'object' ? (a.result as any).ok : undefined;
                            const mark = a.type === 'tool'
                              ? (ok === true ? '✅' : ok === false ? '❌' : '🔧')
                              : a.type === 'thought' ? '💭'
                              : a.type === 'error' ? '⚠️' : '·';
                            return (
                              <div key={j} className="xs" style={{ fontFamily: a.type === 'tool' ? 'var(--font-mono)' : undefined }}>
                                {mark}{' '}
                                {a.type === 'tool'
                                  ? <><b>{a.name}</b>({JSON.stringify(a.args ?? {}).slice(0, 160)}) → {JSON.stringify(a.result ?? '').slice(0, 200)}</>
                                  : (a.text || '').slice(0, 300)}
                              </div>
                            );
                          })}
                        </div>
                      )}
                      <div style={{ fontSize: 13 }}><b>Reply:</b> {(t.response || '').slice(0, 1200) || <span className="muted">(none)</span>}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
    </RuleStateCtx.Provider>
  );
}

// ── CapturedPill — inline "Saved RULE · scope" note (change-scope / undo) ─────
//
// A captured rule is REMEMBERED (rule book) on capture. If it ALSO looks like a
// request to stop asking before commits/deletes (`gate_intent`), the pill shows
// a DISTINCT, explicit opt-in to disable that gate for THIS session or THIS repo
// — never global (global needs the dedicated panel + confirm). The opt-in is the
// ONLY thing that disables a gate; capture itself never does.

const GATE_INTENT_FLAG: Record<string, string> = {
  commit: 'commit_auto_approve',
  delete: 'allow_delete',
};
const GATE_INTENT_LABEL: Record<string, string> = {
  commit: 'Also stop asking before commits?',
  delete: 'Also stop asking before deletes?',
};
const FLAG_LABEL: Record<string, string> = {
  commit_auto_approve: 'commits auto-approved',
  allow_delete: 'deletes auto-approved',
};

function CapturedPill({ item }: { item: CapturedItem }) {
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
      <button onClick={undo} disabled={busy}
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
              <button onClick={() => optIn('session')} disabled={busy}
                style={{ background: 'transparent', border: '1px solid #d4a72c',
                  borderRadius: 4, padding: '1px 8px', fontSize: 11,
                  color: '#d4a72c', cursor: busy ? 'default' : 'pointer' }}>
                This session</button>
              {item.repo && (
                <button onClick={() => optIn('project')} disabled={busy}
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

// ── AutoApprovalsPanel — active gate-disable flags, with Revoke ───────────────
// The audit surface: every disabled gate is visible and revocable here. No way
// to ENABLE a global flag from the UI — that stays a deliberate, separate step.

function AutoApprovalsPanel() {
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
          <button onClick={() => revoke(r)}
            style={{ marginLeft: 'auto', background: 'transparent',
              border: '1px solid var(--border-1)', borderRadius: 4,
              padding: '1px 8px', fontSize: 11, color: 'var(--fg-3)', cursor: 'pointer' }}>
            Revoke</button>
        </div>
      ))}
    </div>
  );
}

// ── AssistantBubble — renders steps + final text ──────────────────────────────

// Thumbnail strip of images attached to the session. Each shows the image, an
// editable description (what makes it queryable when the model can't see it),
// and a delete. A note clarifies whether the model can actually see images.
function MediaStrip({ media, vision, onDescribe, onDelete }: {
  media: ChatMedia[];
  vision: boolean;
  onDescribe: (id: number, d: string) => void;
  onDelete: (id: number) => void;
}) {
  if (!media.length) return null;
  const imgN = media.filter(m => (m.mime || '').startsWith('image/')).length;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '6px 0' }}>
      <div className="xs muted">
        {media.length} file{media.length === 1 ? '' : 's'} in this session
        {imgN > 0 && <> · {vision ? 'model can see images' : 'image descriptions only (model not vision-capable)'}</>}
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {media.map(m => {
          const isImg = (m.mime || '').startsWith('image/');
          return (
          <div key={m.id} style={{ display: 'flex', gap: 6, alignItems: 'flex-start',
                                    border: '1px solid var(--border-1)', borderRadius: 6, padding: 6, maxWidth: 320 }}>
            <a href={chatMediaRawURL(m.id)} target="_blank" rel="noreferrer"
               style={{ textDecoration: 'none' }}>
              {isImg ? (
                <img src={chatMediaRawURL(m.id)} alt={m.filename}
                     style={{ width: 48, height: 48, objectFit: 'cover', borderRadius: 4 }} />
              ) : (
                <div style={{ width: 48, height: 48, display: 'flex', alignItems: 'center',
                              justifyContent: 'center', borderRadius: 4,
                              background: 'var(--bg-2)', color: 'var(--fg-1)' }}>
                  <Icon.File size={22} />
                </div>
              )}
            </a>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div className="xs" style={{ fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.filename}</div>
              {isImg ? (
                <textarea
                  key={`${m.id}:${m.description || ''}`}
                  defaultValue={m.description}
                  placeholder={vision ? 'description (auto/edit)…' : 'describe this image so it can be asked about…'}
                  onBlur={e => { if (e.target.value !== m.description) onDescribe(m.id, e.target.value); }}
                  rows={2}
                  style={{ width: '100%', fontSize: 11, resize: 'vertical', marginTop: 2 }}
                />
              ) : (
                <div className="xs muted" style={{ marginTop: 2 }}>
                  {m.description ? 'text extracted — queryable' : 'no readable text extracted'}
                </div>
              )}
            </div>
            <button className="ghost xs" title="Remove"
                    style={{ padding: '0 4px', cursor: 'pointer' }}
                    onClick={() => onDelete(m.id)}>✕</button>
          </div>
          );
        })}
      </div>
    </div>
  );
}

function AssistantBubble({
  text,
  steps,
  streaming,
  elapsedSec,
  subtasks,
  captured,
  onRegenerate,
}: {
  text: string;
  steps: AgentStep[];
  streaming: boolean;
  elapsedSec?: number;
  subtasks?: SubtaskItem[];
  captured?: CapturedItem[];
  onRegenerate?: () => void;
}) {
  // Agent steps collapse by default once the turn is done (keeps the chat
  // clean — the plan/subtasks + final answer are what matter); auto-expanded
  // while streaming so the live flow is visible.
  const [showSteps, setShowSteps] = useState(streaming);
  return (
    <div>
      {captured && captured.map(c => <CapturedPill key={c.id} item={c} />)}
      {subtasks && subtasks.length > 0 && <SubtaskList items={subtasks} />}
      {steps.length > 0 && (
        <div style={{ marginBottom: text ? 8 : 0 }}>
          <button
            onClick={() => setShowSteps(v => !v)}
            style={{
              background: 'transparent', border: '1px solid var(--border-1)',
              borderRadius: 4, padding: '2px 8px', fontSize: 'var(--fs-xs)',
              color: 'var(--fg-3)', cursor: 'pointer', marginBottom: showSteps ? 6 : 0,
            }}
          >
            {showSteps ? '▾' : '▸'} {steps.length} agent step{steps.length === 1 ? '' : 's'}
          </button>
          {showSteps && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {steps.map((s, i) => (
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
          <button className="ghost xs" title="Copy answer"
                  style={{ padding: '0 4px', cursor: 'pointer' }}
                  onClick={() => { navigator.clipboard
                    ? navigator.clipboard.writeText(text).then(
                        () => toast.success('Copied'), () => toast.error('Copy failed'))
                    : toast.error('Copy needs HTTPS'); }}>
            ⧉ Copy
          </button>
        )}
        {!streaming && onRegenerate && (
          <button className="ghost xs" title="Re-run the previous request"
                  style={{ padding: '0 4px', cursor: 'pointer' }}
                  onClick={onRegenerate}>
            ↻ Regenerate
          </button>
        )}
      </div>
      )}
    </div>
  );
}

// ── Agent step row ─────────────────────────────────────────────────────────────

// Small pill showing WHICH agent produced a step (team mode). Stable
// color per role name so the eye can track each agent across steps.
function AgentBadge({ role }: { role?: string }) {
  if (!role) return null;
  let h = 0;
  for (let i = 0; i < role.length; i++) h = (h * 31 + role.charCodeAt(i)) % 360;
  return (
    <span style={{
      flexShrink: 0,
      fontFamily: 'var(--font-mono)',
      fontStyle: 'normal',
      fontSize: '10px',
      fontWeight: 600,
      padding: '1px 6px',
      borderRadius: 999,
      color: `hsl(${h} 70% 30%)`,
      background: `hsl(${h} 70% 92%)`,
      border: `1px solid hsl(${h} 60% 80%)`,
      marginTop: 1,
      whiteSpace: 'nowrap',
    }} title={`agent: ${role}`}>{role}</span>
  );
}

// A thought/reasoning step. Long chain-of-thought (reasoning models dump
// their whole "Thinking Process…") is collapsed to one line so each
// agent reads as a clean structured step; click to expand the full text.
function ThoughtRow({ step }: { step: Extract<AgentStep, { kind: 'thought' }> }) {
  const long = step.text.length > 180;
  const [open, setOpen] = useState(!long);
  const preview = long && !open
    ? step.text.replace(/\s+/g, ' ').slice(0, 140) + '…'
    : step.text;
  return (
    <div style={{
      display: 'flex', gap: 6, alignItems: 'flex-start',
      padding: '5px 10px',
      background: 'var(--bg-1)',
      border: '1px solid var(--border-0)',
      borderRadius: 'var(--r-sm)',
      fontStyle: 'italic',
      color: 'var(--fg-2)',
      fontSize: 'var(--fs-xs)',
      lineHeight: 1.5,
    }}>
      <span style={{ flexShrink: 0, marginTop: 1 }}>💭</span>
      <AgentBadge role={step.role} />
      <span style={{ whiteSpace: 'pre-wrap', flex: 1 }}>{preview}</span>
      {long && (
        <button
          onClick={() => setOpen(o => !o)}
          className="ghost sm"
          style={{ flexShrink: 0, fontSize: 10, fontStyle: 'normal', padding: '0 6px' }}
        >
          {open ? 'collapse' : 'expand'}
        </button>
      )}
    </div>
  );
}

function DiffBody({ diff }: { diff: string }) {
  return (
    <pre style={{
      margin: 0, padding: '8px 10px', overflowX: 'auto',
      fontSize: 'var(--fs-xs)', lineHeight: 1.55, fontFamily: 'var(--font-mono)',
      background: 'var(--bg-code)', borderTop: '1px solid var(--border-0)',
    }}>
      {diff.split('\n').map((ln, i) => {
        let color: string | undefined;
        let bg: string | undefined;
        if (/^\+\+\+|^---|^diff |^index |^@@/.test(ln)) { color = 'var(--fg-3)'; }
        else if (ln.startsWith('+')) { color = 'var(--ok)'; bg = 'rgba(63,185,80,.10)'; }
        else if (ln.startsWith('-')) { color = 'var(--err)'; bg = 'rgba(248,81,73,.10)'; }
        return <div key={i} style={{ color, background: bg, whiteSpace: 'pre' }}>{ln || ' '}</div>;
      })}
    </pre>
  );
}

function ChangeFileRow({ file }: { file: ChangeFile }) {
  const [open, setOpen] = useState(false);
  const statusColor = file.status === 'added' ? 'var(--ok)'
    : file.status === 'deleted' ? 'var(--err)' : 'var(--accent)';
  return (
    <div style={{ borderTop: '1px solid var(--border-0)' }}>
      <button onClick={() => setOpen(o => !o)} style={{
        width: '100%', display: 'flex', alignItems: 'center', gap: 8,
        padding: '6px 10px', background: 'transparent', border: 0, cursor: 'pointer',
        fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-xs)', textAlign: 'left',
      }}>
        <span style={{ color: 'var(--fg-3)', transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .1s' }}>▸</span>
        <span style={{ color: statusColor, textTransform: 'uppercase', fontSize: 10, width: 62, flexShrink: 0 }}>{file.status}</span>
        <span style={{ color: 'var(--fg-1)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>{file.path}</span>
        {file.additions > 0 && <span style={{ color: 'var(--ok)' }}>+{file.additions}</span>}
        {file.deletions > 0 && <span style={{ color: 'var(--err)' }}>−{file.deletions}</span>}
      </button>
      {open && <DiffBody diff={file.diff} />}
    </div>
  );
}

function ChangesView({ files, summary }: { files: ChangeFile[]; summary: { files: number; additions: number; deletions: number } }) {
  return (
    <div style={{ border: '1px solid var(--border-1)', borderRadius: 'var(--r-md)', overflow: 'hidden', background: 'var(--bg-1)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', background: 'var(--bg-2)', fontSize: 'var(--fs-xs)', fontWeight: 600 }}>
        <Icon.GitBranch size={14} />
        <span>Changes</span>
        <span style={{ color: 'var(--fg-3)', fontWeight: 400 }}>
          {summary.files} file{summary.files === 1 ? '' : 's'}
        </span>
        {summary.additions > 0 && <span style={{ color: 'var(--ok)' }}>+{summary.additions}</span>}
        {summary.deletions > 0 && <span style={{ color: 'var(--err)' }}>−{summary.deletions}</span>}
      </div>
      {files.map((f, i) => <ChangeFileRow key={i} file={f} />)}
    </div>
  );
}

function AgentStepRow({ step }: { step: AgentStep }) {
  if (step.kind === 'changes') {
    return <ChangesView files={step.files} summary={step.summary} />;
  }
  if (step.kind === 'thought') {
    return <ThoughtRow step={step} />;
  }
  if (step.kind === 'tool') {
    const res = step.result as any;
    const ok = res?.ok !== false && !res?.error;
    const snippet = ok
      ? (res?.output ? String(res.output).slice(0, 120) : 'ok')
      : (res?.error ? String(res.error).slice(0, 120) : 'error');
    return (
      <div style={{
        display: 'flex', gap: 6, alignItems: 'flex-start',
        padding: '5px 10px',
        background: 'var(--bg-1)',
        border: '1px solid var(--border-0)',
        borderRadius: 'var(--r-sm)',
        fontSize: 'var(--fs-xs)',
        lineHeight: 1.5,
        fontFamily: 'var(--font-mono)',
        color: ok ? 'var(--fg-1)' : 'var(--err)',
      }}>
        <span style={{ flexShrink: 0, marginTop: 1 }}>🔧</span>
        <AgentBadge role={step.role} />
        <span>
          <strong>{step.name}</strong>
          {'('}
          {Object.entries(step.args as Record<string, unknown>).slice(0, 3).map(([k, v], i) =>
            `${i > 0 ? ', ' : ''}${k}=${JSON.stringify(v).slice(0, 40)}`
          ).join('')}
          {')'}
          {' → '}
          <span style={{ color: ok ? 'var(--ok)' : 'var(--err)' }}>
            {snippet}
          </span>
        </span>
      </div>
    );
  }
  if (step.kind === 'error') {
    return (
      <div style={{
        display: 'flex', gap: 6, alignItems: 'flex-start',
        padding: '5px 10px',
        background: 'var(--err-soft)',
        border: '1px solid transparent',
        borderRadius: 'var(--r-sm)',
        fontSize: 'var(--fs-xs)',
        lineHeight: 1.5,
        color: 'var(--err)',
      }}>
        <span style={{ flexShrink: 0, marginTop: 1 }}>✗</span>
        <span>{step.text}</span>
      </div>
    );
  }
  return null;
}
