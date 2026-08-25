import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import { api, chatApi, chatSessionMessageURL, chatSessionAttachURL, chatSessionStop, chatSessionSteer, chatKillAll, chatMediaUpload, chatMediaList, chatMediaDescribe, chatMediaDelete, ChatMedia, chatSessionSpec, rules as fetchRules, ruleFlags, CapturedRule, GateFlags, ChatSession, ChatMsg, ChatModelEntry } from '../api';
import { Icon } from '../icons';
import { MdLite, copyText as mdCopyText } from '../mdlite';
import { ErrorBoundary } from '../ErrorBoundary';
import { AgentStep, SubtaskItem, RuleState, RuleStateCtx, LiveTurn, ChatMode, BuilderKind, PendingApproval } from './Chat.types';
import { menuBtn, menuItem, LS_SESSION_KEY, LS_MODEL_KEY, LS_MODE_KEY, BUILDER_KINDS, BUILDER_LABELS, LS_BUILDER_KEY, relTime, dateTimeLabel, toAgentStep, msgAwaiting, getDismissedPlans, addDismissedPlan, isStoppedTurn, fmtTokens } from './Chat.helpers';
import { SubtaskList } from './Chat.SubtaskList';
import { ModeBadge } from './Chat.ModeBadge';
import { CtxReload } from './Chat.CtxReload';
import { AutoApprovalsPanel } from './Chat.AutoApprovalsPanel';
import { MediaStrip } from './Chat.MediaStrip';
import { AssistantBubble } from './Chat.AssistantBubble';
import { clickable } from '../a11y';

// Module-level builder-launch guard: epoch-ms of the last ?builder= launch.
// Lives OUTSIDE the component so a lazy-route/Suspense REMOUNT can't reset it
// (a useRef guard did, and one click created 2-3 chats).
let builderLaunchAtMs = 0;

// Trailing " · N msgs" label for a session, or '' when the count is absent/zero.
function msgCountLabel(count?: number | null): string {
  if (count == null || count <= 0) return '';
  const plural = count === 1 ? '' : 's';
  return ` · ${count} msg${plural}`;
}

// Compact display of a session's working directory — truncated from the left
// when long so the meaningful tail stays visible.
function cwdLabel(cwd?: string | null): string {
  if (!cwd) return 'default workspace';
  return cwd.length > 48 ? '…' + cwd.slice(-46) : cwd;
}

// ── Chat component ─────────────────────────────────────────────────────────────

export default function Chat() {
  // Sessions list
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
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
  const [uploadingName, setUploadingName] = useState('');
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
    // Pre-flight size guard (server cap is 50MB) — fail fast with a clear
    // message instead of spinning on a doomed multi-MB POST.
    const MAX = 50 * 1024 * 1024;
    const tooBig = list.find(f => f.size > MAX);
    if (tooBig) { toast.error(`${tooBig.name} is too large (max 50 MB)`); return; }
    // Attach works even on a brand-new chat: create the session first so the
    // file has somewhere to live (mirrors send()).
    let sid = activeId;
    if (sid === null) { sid = await createSession(); if (sid === null) return; }
    setUploadingMedia(true);
    try {
      for (let i = 0; i < list.length; i++) {
        const f = list[i];
        // Show WHICH file + progress so a slow big-doc upload (PDF/Word ->
        // extract + OCR + summary happens server-side before the POST returns)
        // is visibly in-flight, not a silent hang.
        setUploadingName(list.length === 1 ? f.name : `${f.name} (${i + 1}/${list.length})`);
        await chatMediaUpload(sid, f);
      }
      await loadMedia(sid);
      toast.success(list.length === 1
        ? `Attached: ${list[0].name}`
        : `${list.length} files attached`);
    } catch (e: any) {
      toast.error(`Upload failed: ${e.message}`);
    } finally { setUploadingMedia(false); setUploadingName(''); }
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
  // which the user can flip mid-run). All modes are steerable now — used
  // only to word the composer placeholder differently for team.
  const [activeRunMode, setActiveRunMode] = useState<ChatMode>('simple');

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

  // Compaction toggle: turn ALL memory compaction LLM folds off (the daily
  // pass, the boot fold and the sync-loop OKF fold — one switch). Persisted
  // server-side (runtime.env). ENABLED BY DEFAULT now — the rate limiter caps
  // compaction at compaction_rpm (5/min) — so seed the checkbox UNCHECKED to
  // match the real default (unset ⇒ enabled) before the GET resolves.
  const [compactionDisabled, setCompactionDisabled] = useState(false);
  useEffect(() => {
    api.getCompaction().then(r => setCompactionDisabled(!!r.disabled)).catch(() => {});
  }, []);
  async function toggleCompaction(next: boolean) {
    setCompactionDisabled(next);
    try { await api.setCompaction(next); }
    catch { setCompactionDisabled(!next); }   // revert on failure
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

  // Per-mode approval toggle (mirrors Settings → Approvals). ON = this mode
  // pauses for Approve/Reject on risky/file-changing tools; OFF = runs
  // uninterrupted (hard DENY + destructive-delete still confirm).
  const [approvalFlags, setApprovalFlags] =
    useState<{ chat: boolean; plan: boolean; pipeline: boolean }>(
      { chat: true, plan: true, pipeline: true });
  useEffect(() => {
    chatApi.approvalSettings().then(setApprovalFlags).catch(() => { /* */ });
  }, []);
  const modeApprovalKey = (m: ChatMode): 'chat' | 'plan' | 'pipeline' => {
    if (m === 'team') return 'pipeline';
    if (m === 'plan') return 'plan';
    return 'chat';
  };
  const approvalsOn = approvalFlags[modeApprovalKey(chatMode)];
  async function toggleApprovals() {
    const key = modeApprovalKey(chatMode);
    const next = !approvalFlags[key];
    setApprovalFlags(f => ({ ...f, [key]: next }));   // optimistic
    try {
      setApprovalFlags(await chatApi.setApprovalMode(key, next));
      toast.success(`${key} approvals ${next ? 'on' : 'off'}`);
    } catch (e: any) {
      setApprovalFlags(f => ({ ...f, [key]: !next }));
      toast.error(e?.message || 'Failed to update approvals');
    }
  }

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
  function clearBuilderForSession(id: number) {
    setBuilderBySession(prev => {
      if (!(id in prev)) return prev;
      const n = { ...prev }; delete n[id]; return n;
    });
  }
  function setBuilderForSession(id: number, kind: BuilderKind) {
    setBuilderBySession(prev => ({ ...prev, [id]: kind }));
  }

  // Launch a builder from a `?builder=<kind>` query param: create a fresh session
  // in that builder mode. TWO defenses against double-creates (one click used
  // to open 2-3 chats — reproduced live):
  //   1. Clear the param FIRST, synchronously — the old code cleared it only
  //      AFTER the async create, so a Chat REMOUNT in that window (lazy route +
  //      Suspense swap) saw the param still set with a fresh useRef and created
  //      ANOTHER session.
  //   2. A MODULE-level, time-boxed guard (below, outside the component) that
  //      survives remounts — a useRef dies with each instance. Time-boxed so a
  //      genuine later "New workflow via chat" click still opens a fresh chat.
  useEffect(() => {
    const b = searchParams.get('builder');
    if (!b) return;
    if (!BUILDER_KINDS.includes(b as BuilderKind)) {
      setSearchParams({}, { replace: true });
      return;
    }
    const now = Date.now();
    if (now - builderLaunchAtMs < 3000) return;  // remount/double-fire dedupe
    builderLaunchAtMs = now;
    setSearchParams({}, { replace: true });      // clear BEFORE the async create
    (async () => {
      const id = await createSession();
      if (id !== null) setBuilderForSession(id, b as BuilderKind);
      setTimeout(() => textareaRef.current?.focus(), 50);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Pre-apply "Review edits" gate: OFF by default — file writes/patches
  // auto-apply with no per-edit Approve/Reject prompt. Operators who want the
  // gate back can force it server-side with AIFORGE_CHAT_REVIEW_EDITS=1.
  // (Team/parallel runs never hold edits regardless.)
  const reviewEdits = false;
  // Quick mode: cap the agent at a handful of steps for small asks. Persisted
  // like the mode itself, because someone who wants it usually wants it for a
  // run of small edits, not for one message.
  const [quickMode, setQuickMode] = useState<boolean>(() => {
    try { return localStorage.getItem('aiforge.chat.quick') === '1'; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem('aiforge.chat.quick', quickMode ? '1' : '0'); } catch { /* ignore */ }
  }, [quickMode]);
  // Cave mode (lean context) is now AUTO-enabled for small model windows
  // (≤48K) server-side, so the per-chat toggle was removed — it's the
  // default for local models. Advanced operators can still force it on/off
  // from the Settings LLM card.

  // Pending approval gate (#1) + checkpoints panel (#3).
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  // Optional guidance the user types on the approval card — on reject it steers
  // the agent ("use single-star bold") so it adjusts + continues, no separate
  // follow-up message needed.
  const [approvalNote, setApprovalNote] = useState('');
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
  // Bounded auto-reconnect: a long run's SSE can drop client-side (browser tab
  // throttle, a proxy idle cap) even though the run keeps going server-side and
  // is fully replayable via /attach. On such a drop we re-attach instead of
  // showing "network error"; this caps the retries so a genuinely dead run
  // still surfaces the error rather than looping.
  const reconnectRef = useRef<number>(0);

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
    const note = approvalNote.trim();
    setPendingApproval(null);   // optimistic — run resumes server-side
    setApprovalNote('');
    if (!p || activeId === null) return;
    if (p.sessionId !== activeId) return;   // stale card from another session
    try {
      await chatApi.approve(p.sessionId, decision, p.id, note || undefined);
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
      // Defensive prune: drop builder mappings whose session no longer exists,
      // so an orphaned entry can't later collide with a recycled id and open a
      // normal chat in builder mode.
      const _alive = new Set(list.map((s: ChatSession) => s.id));
      setBuilderBySession(prev => {
        const kept: Record<number, BuilderKind> = {};
        let changed = false;
        for (const [k, v] of Object.entries(prev)) {
          if (_alive.has(Number(k))) kept[Number(k)] = v; else changed = true;
        }
        return changed ? kept : prev;
      });
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
      // Map the server-persisted per-turn duration → elapsedSec so EVERY turn
      // (simple/plan/team) shows its time-taken after reload, not just live.
      setMessages((res.messages || []).map((m: any) =>
        m.duration_s != null && m.elapsedSec === undefined
          ? { ...m, elapsedSec: m.duration_s } : m));
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
      // Drop any builder mapping for this id — else a future reset recycles the
      // id and a fresh NORMAL chat would inherit this builder mode (job/rule/…).
      setBuilderBySession(prev => {
        if (!(id in prev)) return prev;
        const n = { ...prev }; delete n[id]; return n;
      });
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
        // MERGE, never replace: the end-of-turn event carries only the settled
        // request counts, and overwriting would blank the context meter with it.
        setLiveTurn(prev => prev ? { ...prev, usage: {
          // budget 0 marks "no context reading yet" — the context bar is
          // guarded on it, so a count-only event (Stop before the first
          // in-loop usage) cannot render an empty "0k / 0k (0%)" meter.
          ...(prev.usage || { pct: 0, chars: 0, budget: 0 }),
          ...(evt.pct !== undefined ? {
            pct: evt.pct, chars: evt.context_chars, budget: evt.budget_chars,
            tokens: evt.context_tokens, windowTokens: evt.window_tokens,
          } : {}),
          ...(evt.llm_turn !== undefined ? {
            llmTurn: evt.llm_turn, llmSession: evt.llm_session,
            llmPerMin: evt.llm_per_min,
            // `?? 0`, not the raw value: an API that predates these fields
            // would otherwise leave the previous event's failure count
            // standing on a turn that never reported one.
            llmTurnFailed: evt.llm_turn_failed ?? 0,
            llmFailedPerMin: evt.llm_failed_per_min ?? 0,
            llmTurnTokensOut: evt.llm_turn_tokens_out ?? 0,
          } : {}),
        } } : prev);
        return;
      }

      // Plan ready (Gap B): a plan-mode run produced an approvable spec.
      if (evt.type === 'plan_ready') {
        setPlanReady({ spec: evt.spec || '' });
        return;
      }

      // Builder finalized (job/skill/workflow/rule created): drop this session's
      // builder mode so the NEXT message is a normal chat, not another interview.
      if (evt.type === 'builder_done') {
        if (activeIdRef.current !== null) clearBuilderForSession(activeIdRef.current);
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
        if (evt.type === 'tool_start') {
          // Live "it's running" row — flipped to the real result by the
          // matching 'tool' event below (matched on call_id) instead of
          // showing nothing for however long a slow bash/test/build takes.
          return { ...prev, steps: [...prev.steps, { kind: 'tool' as const, name: evt.name, args: evt.args || {}, result: {}, role: evt.role, pending: true, call_id: evt.call_id }] };
        }
        if (evt.type === 'tool') {
          const idx = evt.call_id !== undefined
            ? prev.steps.findIndex(s => s.kind === 'tool' && s.pending && s.call_id === evt.call_id)
            : -1;
          if (idx !== -1) {
            const steps = [...prev.steps];
            steps[idx] = { kind: 'tool' as const, name: evt.name, args: evt.args || {}, result: evt.result || {}, role: evt.role, call_id: evt.call_id };
            return { ...prev, steps };
          }
          // No matching pending row (hook-blocked / rejected / cancelled path
          // never emits tool_start) — append, same as before this change.
          return { ...prev, steps: [...prev.steps, { kind: 'tool' as const, name: evt.name, args: evt.args || {}, result: evt.result || {}, role: evt.role }] };
        }
        if (evt.type === 'changes') {
          return { ...prev, steps: [...prev.steps, { kind: 'changes' as const, files: evt.files || [], summary: evt.summary || { files: (evt.files || []).length, additions: 0, deletions: 0 } }] };
        }
        if (evt.type === 'message') {
          // A supplementary message (build/integration report) is shown as an
          // extra step — it must NOT replace the agent's actual answer text.
          if (evt.supplementary) {
            return { ...prev, steps: [...prev.steps, { kind: 'message' as const, text: evt.text, role: evt.role }] };
          }
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
        reconnectRef.current = 0;   // resumed + reconciled — reset the budget
        if (timerRef.current !== null) { clearInterval(timerRef.current); timerRef.current = null; }
        await loadSession(sessionId);
        setLiveTurn(null);
        loadSessions(true);
      }
    } catch (e: any) {
      // Abort (navigated away / session switch / superseded by send) is
      // expected — not an error. A mid-stream drop of a run that's still live
      // server-side is retried (bounded) so a flaky connection resumes rather
      // than leaving a spinner that never resolves.
      if (e?.name !== 'AbortError' && abortRef.current === ctrl
          && activeIdRef.current === sessionId && reconnectRef.current < 3) {
        reconnectRef.current += 1;
        setTimeout(() => { if (activeIdRef.current === sessionId) attachToRun(sessionId); }, 900);
      }
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

  async function send(overrideContent?: string, overrideMode?: ChatMode,
                      opts?: { resume?: boolean }) {
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
    // True once the run's SSE has opened (POST returned ok). A failure AFTER
    // this is a mid-stream drop of a run that's alive server-side → reattach.
    // A failure BEFORE (a non-ok POST) is a real start error → show it.
    let streamOpened = false;
    try {
      const res = await fetch(chatSessionMessageURL(sessionId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: q, mode: builder ? 'simple' : runMode, review_edits: reviewEdits,
                               // Quick only applies to the single-agent modes;
                               // Team runs its own pipeline and ignores it.
                               quick: runMode !== 'team' ? quickMode : false,
                               // Resume a stopped turn rather than redo it. The
                               // server also infers this when the same words are
                               // re-sent; the flag covers a rephrase.
                               ...(opts?.resume ? { resume: true } : {}),
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

      streamOpened = true;
      await pumpStream(res, sessionId);
      reconnectRef.current = 0;   // clean completion — reset the retry budget

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
      } else if (streamOpened && sessionId != null && reconnectRef.current < 3) {
        // The SSE dropped mid-run but the run is alive + replayable server-side.
        // Re-attach instead of surfacing a spurious "network error". Bounded so a
        // genuinely dead run still falls through to the error below. Done after
        // this function's finally (which clears busy/abortRef) so attachToRun's
        // busy guard passes.
        reconnectRef.current += 1;
        setLiveTurn(prev => prev ? { ...prev, streaming: true } : prev);
        setTimeout(() => { if (activeIdRef.current === sessionId) attachToRun(sessionId); }, 900);
      } else {
        reconnectRef.current = 0;
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
        // Show the steer text IMMEDIATELY as a step in the live stream so it
        // doesn't just vanish from the composer until the agent drains it. It
        // commits with the turn (no orphan): the later server role:'steer' echo
        // marks it actually applied.
        setLiveTurn(prev => prev ? { ...prev, steps: [...prev.steps, {
          kind: 'thought' as const, role: 'steer',
          text: `↪ Steer queued (applies at the next step): ${q}`,
        }] } : prev);
        toast('Steer queued — applies at the next step');
      } else if (r.unsupported) {
        setInput(q);   // restore — nothing was queued
        toast('Steering not available for this run');
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
  // A turn the server stopped (runaway cap / turn deadline / Stop) leaves its
  // banner as the assistant text. Retrying one is a RESUME, not a re-ask.
  const lastTurnStopped = isStoppedTurn(lastAssistantMsg);
  // M2: re-run the last request. After a stopped turn the server prepends a
  // brief of what already landed + what is still pending, so this finishes the
  // job instead of starting it again.
  function regenerate() {
    if (busy || !lastUserMsg?.content) return;
    send(lastUserMsg.content, undefined, { resume: lastTurnStopped });
  }
  // The escape hatch: the partial work may be junk the user wants abandoned.
  // Without this, every route back to "run this again" meant "continue this".
  function rerunFresh() {
    if (busy || !lastUserMsg?.content) return;
    send(lastUserMsg.content, undefined, { resume: false });
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
  // M2: copy with uniform feedback. Uses mdlite.copyText, which falls back to a
  // hidden-textarea execCommand when the clipboard API is unavailable (plain
  // HTTP on a LAN IP) — so Copy works everywhere, not only over HTTPS.
  function copyText(t: string) {
    mdCopyText(t).then(() => toast.success('Copied'),
                       () => toast.error('Copy failed'));
  }
  const isLastTurn = messages.length > 0 && lastAssistantMsg === messages[messages.length - 1];
  const persistedAwaiting = !!(isLastTurn && lastAssistantMsg && msgAwaiting(lastAssistantMsg));
  // The current turn is waiting for the user to answer — Enter/primary button
  // must SEND a reply (a normal turn), not steer.
  const awaitingReply = !!liveTurn?.awaiting || persistedAwaiting;
  // Steering is valid while genuinely running (not awaiting a reply, not
  // gated on an approval) — every mode drains the queue now: simple/plan's
  // ReAct loop, the parallel-team subtask loop (folds into SPEC.md), and
  // the sequential team ADK driver's Doer/Refiner before_model callback.
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
    <div className="chat-shell-v2">
      {/* ── Left sidebar: sessions list ─────────────────────────────────────── */}
      <div className="chat-sessions-sidebar">
        <div className="chat-sessions-header" style={{ display: 'flex', gap: 6 }}>
          <button type="button" onClick={handleNewChat} disabled={busy} style={{ flex: 1 }}>
            <Icon.Plus size={13} /> New chat
          </button>
          {sessions.length > 0 && (
            <button type="button"
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
                  // Reset RECYCLES session ids, so every stale builder mapping
                  // would now collide with a brand-new normal chat (id 1, 2, …)
                  // and wrongly open it as a job/rule/workflow builder. Wipe them.
                  setBuilderBySession({});
                  localStorage.removeItem(LS_BUILDER_KEY);
                  await loadSessions();
                } catch (e: any) { toast.error(e.message); }
              }}
            >
              <Icon.Trash size={13} />
            </button>
          )}
        </div>
        <div className="chat-sessions-list">
          {(() => {
            if (sessionsLoading) return (
            <div style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {[1,2,3].map(i => (
                <div key={i} className="skeleton" style={{ height: 44, borderRadius: 8 }} />
              ))}
            </div>
            );
            if (sessions.length === 0) return (
            <div style={{ padding: '16px 10px', textAlign: 'center', color: 'var(--fg-3)', fontSize: 'var(--fs-xs)' }}>
              No conversations yet
            </div>
            );
            return sessions.map(s => (
            <div
              key={s.id}
              className={`chat-session-item ${s.id === activeId ? 'active' : ''}`}
              {...clickable(() => { if (!renaming || renaming.id !== s.id) selectSession(s.id); })}
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
                    <div className="chat-session-title" title={s.title}>
                      {s.title || 'Untitled'}
                      <ModeBadge mode={s.last_mode} />
                    </div>
                    <div className="chat-session-meta">
                      <span title={new Date(s.updated_at).toLocaleString()}>
                        {dateTimeLabel(s.updated_at)} · {relTime(s.updated_at)}
                      </span>
                      {msgCountLabel(s.message_count)}
                    </div>
                  </>
                )}
              </div>
              {(!renaming || renaming.id !== s.id) && (
                <div className="chat-session-actions">
                  <button type="button"
                    title="Rename"
                    onClick={e => startRename(s, e)}
                  >
                    ✎
                  </button>
                  <button type="button"
                    title="Delete"
                    onClick={e => { e.stopPropagation(); deleteSession(s.id); }}
                  >
                    <Icon.X size={11} />
                  </button>
                </div>
              )}
            </div>
          ));
          })()}
        </div>
      </div>

      {/* ── Main pane ──────────────────────────────────────────────────────────── */}
      <div className="chat-v2-main">
        {/* Topbar */}
        <div className="chat-topbar">
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: 1 }}>
            {/* Title — click to rename (no separate button). */}
            <button type="button"
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
                {cwdLabel(activeSession.cwd)}
              </span>
            )}
            {activeBuilder && (
              <span className="chip ok" title="Click to exit builder mode and chat normally"
                    {...clickable(() => { if (activeId !== null) clearBuilderForSession(activeId); })}
                    style={{ alignSelf: 'flex-start', marginTop: 2, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                <Icon.Sparkles size={11} /> {BUILDER_LABELS[activeBuilder]} · exit ✕
              </span>
            )}
          </div>
          <div className="row" style={{ gap: 'var(--s-2)' }}>
            {/* Simple | Plan | Team mode toggle */}
            <div className="chat-mode-toggle" title="Simple: single agent · Plan: read-only, proposes a plan first · Team: full ADK planner→doer→learner pipeline">
              <button type="button"
                className={chatMode === 'simple' ? 'active' : ''}
                onClick={() => setChatMode('simple')}
                disabled={busy}
              >
                Simple
              </button>
              <button type="button"
                className={chatMode === 'plan' ? 'active' : ''}
                onClick={() => setChatMode('plan')}
                disabled={busy}
                title="Read-only: the agent inspects the repo and proposes a plan; switch to Simple/Team to execute"
              >
                Plan
              </button>
              <button type="button"
                className={chatMode === 'team' ? 'active' : ''}
                onClick={() => setChatMode('team')}
                disabled={busy}
              >
                Team (full flow)
              </button>
            </div>

            {/* Quick: one doer, hard step cap. Hidden in Team, which runs the
                full pipeline and has nothing to cap. */}
            {chatMode !== 'team' && (
              <button
                type="button"
                onClick={() => setQuickMode(v => !v)}
                disabled={busy}
                title={quickMode
                  ? 'Quick ON — a single doer with a hard step cap. Best for a rename, a one-line fix or a question, where the agent\'s own exploration costs more than the change. Click to turn OFF.'
                  : 'Quick OFF — the agent works until it is done (normal). Turn ON for small asks that should not take minutes.'}
                style={{
                  display: 'flex', alignItems: 'center', gap: 5, fontSize: 'var(--fs-xs)',
                  padding: '2px 8px', borderRadius: 999, cursor: 'pointer',
                  border: `1px solid ${quickMode ? 'var(--accent,#2f81f7)' : 'var(--border-1)'}`,
                  background: quickMode ? 'rgba(47,129,247,0.10)' : 'transparent',
                  color: quickMode ? 'var(--accent,#2f81f7)' : 'var(--fg-2)',
                }}
              >
                {quickMode ? '⚡' : '🐢'} Quick {quickMode ? 'on' : 'off'}
              </button>
            )}

            {/* Per-mode approval toggle — pause for Approve/Reject in this mode */}
            <button
              type="button"
              onClick={toggleApprovals}
              title={approvalsOn
                ? `Approvals ON for ${modeApprovalKey(chatMode)} — pauses for Approve/Reject on risky/file-changing tools. Click to turn OFF.`
                : `Approvals OFF for ${modeApprovalKey(chatMode)} — runs uninterrupted (hard-denied actions + destructive deletes still confirm). Click to turn ON.`}
              style={{
                display: 'flex', alignItems: 'center', gap: 5, fontSize: 'var(--fs-xs)',
                padding: '2px 8px', borderRadius: 999, cursor: 'pointer',
                border: `1px solid ${approvalsOn ? 'var(--ok,#22c55e)' : 'var(--border-1)'}`,
                background: approvalsOn ? 'rgba(34,197,94,0.10)' : 'transparent',
                color: approvalsOn ? 'var(--ok,#22c55e)' : 'var(--fg-2)',
              }}
            >
              {approvalsOn ? '🛡' : '⚡'} Approvals {approvalsOn ? 'on' : 'off'}
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
                />{' '}
                Model{' '}
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
                Orchestrator{' '}
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
              <button type="button" className="ghost sm" onClick={() => setMenuOpen(o => !o)}
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
                             onChange={e => toggleFullPipeline(e.target.checked)} disabled={busy} />{' '}
                      Force full pipeline
                    </label>
                  )}
                  <label className="chat-menu-item" title="Turn off the daily memory-compaction pass (recompact + dedupe + evening fold). Takes effect on next restart."
                         style={menuItem}>
                    <input type="checkbox" checked={compactionDisabled}
                           onChange={e => toggleCompaction(e.target.checked)} disabled={busy} />{' '}
                    Disable memory compaction
                  </label>
                  {activeSession && (
                    <button type="button" style={menuBtn} onClick={() => { setMenuOpen(false); openCheckpoints(); }}>
                      ↶ Checkpoints
                    </button>
                  )}
                  <button type="button" style={menuBtn} onClick={() => { setMenuOpen(false); killAll(); }}
                          title="Force-stop every run + release the team lock">
                    ⚠ Reset stuck runs
                  </button>
                  {activeSession && (
                    <>
                      <div style={{ height: 1, background: 'var(--border-1)', margin: '4px 0' }} />
                      <button type="button" style={{ ...menuBtn, color: 'var(--err)' }}
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
            <button type="button" onClick={handleNewChat}>
              <Icon.Plus size={14} /> New chat
            </button>
          </div>
        ) : (() => {
          if (msgsLoading) return (
          <div className="chat-log" style={{ justifyContent: 'center', alignItems: 'center' }}>
            <div className="typing"><span /><span /><span /></div>
          </div>
          );
          return (
          <>
            <div className="chat-log" ref={logRef}>
              {/* Captured auto-approve flags are IGNORED while this mode requires
                  approval, so don't advertise them then — only surface the panel
                  when approvals are OFF and a bypass can actually take effect. */}
              {!approvalsOn && <AutoApprovalsPanel />}
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
                          stopped={msg === lastAssistantMsg && lastTurnStopped}
                          onRerunFresh={msg === lastAssistantMsg && !busy && lastTurnStopped
                                          ? rerunFresh : undefined}
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
                            <button type="button" className="ghost xs"
                                    title={msg === lastUserMsg
                                      ? 'Edit this message in the composer'
                                      : 'Edit & resend from here — replaces this and all later turns'}
                                    style={{ padding: '0 4px', cursor: 'pointer' }}
                                    onClick={() => editUserMessage(msg)}>✎ Edit</button>
                            <button type="button" className="ghost xs" title="Copy"
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
                    {/* Requests actually sent to the LLM. One question is
                        rarely one call — every ReAct step, retry and condense
                        is a request — and that surprise is what people report
                        as "request overload". Stays visible after the turn
                        ends, unlike the context meter. */}
                    {liveTurn.usage?.llmTurn !== undefined && (
                      <div className="xs muted" style={{ marginTop: 4, display: 'flex', alignItems: 'center', gap: 6 }}
                           title={`${liveTurn.usage.llmTurn} request(s) to the model for this message · `
                                  + `${liveTurn.usage.llmSession ?? 0} in this chat since the server started · `
                                  + `${liveTurn.usage.llmPerMin ?? 0}/min across all chats right now. `
                                  + ((liveTurn.usage.llmTurnFailed ?? 0) > 0
                                      ? `${liveTurn.usage.llmTurnFailed} of this message's requests came back `
                                        + `with no answer (timeout, error or empty) and were retried. `
                                      : '')
                                  + `Counted at the wire, so retries count too.`}>
                        <span>⚡ {liveTurn.usage.llmTurn} LLM {liveTurn.usage.llmTurn === 1 ? 'request' : 'requests'}</span>
                        {(liveTurn.usage.llmTurnTokensOut ?? 0) > 0 && (
                          // What the model WROTE. The request count cannot say
                          // it: 40 one-line steps and one 6000-token essay are
                          // both "41 requests".
                          <span>· {fmtTokens(liveTurn.usage.llmTurnTokensOut ?? 0)} written</span>
                        )}
                        {(liveTurn.usage.llmTurnFailed ?? 0) > 0 && (
                          // Named, not netted out: "12 requests, 7 failed" is a
                          // retry storm; "12 requests" alone reads as a
                          // thorough turn.
                          <span style={{ color: 'var(--err,#e5534b)' }}>
                            · {liveTurn.usage.llmTurnFailed} failed
                          </span>
                        )}
                        {(liveTurn.usage.llmSession ?? 0) > (liveTurn.usage.llmTurn ?? 0) && (
                          <span>· {liveTurn.usage.llmSession} this chat</span>
                        )}
                        {(liveTurn.usage.llmPerMin ?? 0) > 0 && (
                          <span style={{ color: (liveTurn.usage.llmPerMin ?? 0) > 60 ? 'var(--err,#e5534b)' : undefined }}>
                            · {liveTurn.usage.llmPerMin}/min
                          </span>
                        )}
                      </div>
                    )}
                    {/* M3: context-window fill meter while streaming */}
                    {liveTurn.streaming && liveTurn.usage
                      && (liveTurn.usage.budget ?? 0) > 0 && (
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
                  // STICKY so a pending Approve/Reject is ALWAYS visible at the
                  // bottom of the chat as it streams — an inline card scrolls
                  // out of view and gets ignored. Pinned above the composer with
                  // a strong warn border + shadow so it reads as "act now".
                  position: 'sticky', bottom: 8, zIndex: 30,
                  margin: '8px 0', padding: 12,
                  border: '2px solid var(--warn, #f59e0b)',
                  borderRadius: 8, background: 'var(--bg-1)',
                  boxShadow: '0 6px 24px rgba(0,0,0,0.28)',
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
                  {/* Guidance box — chat on the approval. Text here steers the
                      agent on Reject (it adjusts + continues); optional. */}
                  <textarea
                    value={approvalNote}
                    onChange={e => setApprovalNote(e.target.value)}
                    placeholder="Optional guidance — e.g. 'use single-star bold'. On Reject this steers the agent to fix + continue."
                    rows={2}
                    onKeyDown={e => {
                      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                        e.preventDefault();
                        resolveApproval(approvalNote.trim() ? 'reject' : 'approve');
                      }
                    }}
                    style={{ width: '100%', boxSizing: 'border-box', fontSize: 12,
                             padding: 6, borderRadius: 6, marginBottom: 8,
                             border: '1px solid var(--fg-3)', background: 'var(--bg-2)',
                             color: 'var(--fg-1)', resize: 'vertical' }}
                  />
                  {/* Buttons FIRST — always reachable without scrolling past a
                      tall preview (the old layout buried them under the diff). */}
                  <div style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
                    <button type="button" onClick={() => resolveApproval('approve')}>✓ Approve</button>
                    <button type="button" className="danger" onClick={() => resolveApproval('reject')}>
                      {approvalNote.trim() ? '✗ Reject + send guidance' : '✗ Reject'}
                    </button>
                  </div>
                  {pendingApproval.preview && (
                    // Collapsible so the change PREVIEW (a diff / page body) is
                    // available but doesn't dominate the card as a wall of code.
                    <details open>
                      <summary style={{ cursor: 'pointer', fontSize: 12,
                                        color: 'var(--fg-2)', marginBottom: 6 }}>
                        Preview changes
                      </summary>
                      <div style={{
                        maxHeight: '50vh', overflow: 'auto', fontSize: 12,
                        background: 'var(--bg-2)', padding: 8, borderRadius: 6,
                      }}>
                        {/* markdown (Confluence/Jira page bodies) + ```diff
                            fences MdLite colors. Container scrolls. */}
                        <MdLite text={pendingApproval.preview} />
                      </div>
                    </details>
                  )}
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
                {/* Its OWN boundary. This dock is fed by six different
                    producers' event payloads, and a single bad field in one of
                    them took the ENTIRE chat view down — messages, composer
                    and all — because the only boundary is around the whole
                    route. A panel should degrade to a panel. */}
                <ErrorBoundary fallback={(e, reset) => (
                  <div className="small muted" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <span>Tasks panel failed to render ({String(e.message).slice(0, 80)})</span>
                    <button type="button" className="ghost xs" onClick={reset}>retry</button>
                  </div>
                )}>
                  <SubtaskList items={dockSubtasks} onViewSpec={openSpec} />
                </ErrorBoundary>
              </div>
            )}

            {/* SPEC.md preview modal */}
            {specModal && (
              <div {...clickable(() => setSpecModal(null))} aria-label="Close"
                   style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
                            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
                <div // Not a control: this panel exists only to stop the overlay's click
              // from closing the dialog. A matching key handler keeps that
              // true for keyboard users without announcing it as a button.
              onClick={(e) => e.stopPropagation()}
                     onKeyDown={(e) => e.stopPropagation()}
                     style={{ background: 'var(--bg-0,#0d1017)', border: '1px solid var(--border,#2a2f3a)',
                              borderRadius: 8, width: 'min(820px, 92vw)', maxHeight: '85vh', overflowY: 'auto',
                              padding: '18px 22px', boxShadow: '0 12px 40px rgba(0,0,0,0.5)' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                    <strong>📄 SPEC.md</strong>
                    <button type="button" className="ghost" style={{ cursor: 'pointer' }} onClick={() => setSpecModal(null)}>✕</button>
                  </div>
                  {specModal.loading ? <div className="muted">Loading…</div>
                    : <MdLite text={specModal.content} />}
                </div>
              </div>
            )}

            {uploadingMedia && (
              <div className="upload-banner" role="status" aria-live="polite">
                <span className="af-spin"><Icon.Refresh size={14} /></span>
                <span>Uploading &amp; analyzing{uploadingName ? ` ${uploadingName}` : ''}…</span>
                <span className="muted" style={{ fontWeight: 400 }}>large PDFs / Word docs may take a moment</span>
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
                  <button type="button" className="ghost xs" style={{ cursor: 'pointer' }}
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
                  placeholder={(() => {
                    if (pendingApproval) return "Resolve the approval above first (Approve / Reject)…";
                    if (awaitingReply) return "The agent is waiting for your reply — type your answer, Enter to send…";
                    if (busy) return activeRunMode === 'team'
                      ? "Steer the run — your note is folded into SPEC.md for the remaining tasks (Enter to send)…"
                      : "Steer the running agent — type guidance, Enter to inject (no Stop needed)…";
                    return "Ask the agent to read/write files, run commands, implement a feature…  (Enter to send, Shift+Enter for newline)";
                  })()}
                  value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={onKey}
                  style={{ flex: 1, resize: 'vertical', minHeight: busy ? 34 : 64 }}
                />
                <button type="button" onClick={() => mediaInputRef.current?.click()} disabled={uploadingMedia}
                        title={uploadingMedia ? 'Uploading & analyzing…' : 'Attach a file — image, PDF, Word, Excel, text — queryable all session'}
                        style={{ whiteSpace: 'nowrap', display: 'inline-flex', alignItems: 'center' }}>
                  {uploadingMedia
                    ? <span className="af-spin"><Icon.Refresh size={15} /></span>
                    : <Icon.Paperclip size={15} />}
                </button>
                {busy && (
                  <button type="button" onClick={stopRun} className="danger"
                          title="Stop all agents + processes for this run"
                          style={{ whiteSpace: 'nowrap' }}>
                    ■ Stop
                  </button>
                )}
                {(() => {
                  if (canSteer) return (
                  <button type="button" onClick={steer} disabled={!input.trim() || steering}
                          title="Inject this guidance into the running agent without stopping it"
                          style={{ whiteSpace: 'nowrap' }}>
                    ↳ Steer
                  </button>
                  );
                  if (busy && !awaitingReply) return (
                  // canSteer is false here only because pendingApproval is set
                  // (the review-edits gate — simple/plan only, team doesn't use
                  // it, so this is never actually a team-mode restriction).
                  <button type="button" disabled
                          title="Resolve the approval above before steering"
                          style={{ whiteSpace: 'nowrap' }}>
                    ↳ Steer
                  </button>
                  );
                  return (
                  // Idle, or awaiting the user's reply — primary action sends a
                  // normal turn (FE1).
                  <button type="button" onClick={() => send()} disabled={busy || !input.trim()}>
                    <Icon.Agents size={14} /> {awaitingReply ? 'Reply' : 'Run'}
                  </button>
                  );
                })()}
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
                  <button type="button" onClick={() => {
                            // FE3: remember the dismissal so loadSession (reload /
                            // session switch / Stop) doesn't resurrect the pill.
                            if (activeId !== null && planReady.msgId != null) {
                              addDismissedPlan(activeId, planReady.msgId);
                            }
                            setPlanReady(null);
                          }} className="ghost"
                          style={{ whiteSpace: 'nowrap' }}>Dismiss</button>
                  <button type="button" onClick={() => send(planReady.spec, 'team')}
                          title="Run the approved plan as a full team build"
                          style={{ whiteSpace: 'nowrap' }}>
                    ✓ Approve &amp; Execute
                  </button>
                </div>
              )}
            </div>
          </>
          );
        })()}

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
              <button type="button" onClick={() => mediaInputRef.current?.click()} disabled={uploadingMedia}
                      title="Attach a file — image, PDF, Word, Excel, text — starts a chat, queryable all session"
                      style={{ whiteSpace: 'nowrap', display: 'inline-flex', alignItems: 'center' }}>
                {uploadingMedia ? '…' : <Icon.Paperclip size={15} />}
              </button>
              {busy && (
                <button type="button" onClick={stopRun} className="danger"
                        title="Stop all agents + processes for this run"
                        style={{ whiteSpace: 'nowrap' }}>
                  ■ Stop
                </button>
              )}
              <button type="button" onClick={() => send()} disabled={busy || !input.trim()}>
                <Icon.Agents size={14} /> Run
              </button>
            </div>
          </div>
        )}

        {/* Checkpoints panel (#3) */}
        {checkpoints !== null && (
          <div
            {...clickable(() => setCheckpoints(null))} aria-label="Close"
            style={{
              position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50,
            }}
          >
            <div onClick={e => e.stopPropagation()}
                 onKeyDown={e => e.stopPropagation()} style={{
              width: 'min(560px, 92vw)', maxHeight: '70vh', overflow: 'auto',
              background: 'var(--bg-0)', border: '1px solid var(--border-1)',
              borderRadius: 10, padding: 16,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                <strong>Workspace checkpoints</strong>
                <button type="button" className="ghost sm" onClick={() => setCheckpoints(null)}><Icon.X size={12} /></button>
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
                        <button type="button" className="ghost sm" onClick={() => restoreCheckpoint(c.sha)} title="Revert tracked files to this snapshot; keep files created after it">↶ Restore</button>
                        <button type="button" className="ghost sm danger" onClick={() => restoreCheckpoint(c.sha, true)} title="Full restore: make the tree exactly match this snapshot — deletes files created after it">⤓ Full</button>
                      </div>
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
