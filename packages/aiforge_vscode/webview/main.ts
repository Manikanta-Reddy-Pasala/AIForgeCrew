// The chat webview: keeps the chat state, applies run events with the SAME
// reducer as the web UI (web/src/views/Chat.reduce.ts), and posts the user's
// actions to the extension. Rendering is batched to one frame so a stream of
// deltas does not rebuild the log per token.
import type { LiveTurn } from '../../../web/src/views/Chat.model';
import { reduceTurn } from '../../../web/src/views/Chat.reduce';
import { STYLE } from './style';
import { FileChange, Msg, renderApproval, renderHistory, renderLive, turnChanges } from './view';

declare function acquireVsCodeApi(): { postMessage(m: unknown): void };
const vscode = acquireVsCodeApi();

const state = {
  messages: [] as Msg[],
  live: null as LiveTurn | null,
  pendingUser: null as string | null,
  running: false,
  approval: null as any,
  error: '' as string,
  mode: 'simple',
  cwd: '',
  scratch: false,
  replaceFrom: null as number | null,
  notice: '',
};

const app = document.getElementById('app')!;
app.innerHTML = `<style>${STYLE}</style>
  <div id="log"></div>
  <div id="composer">
    <div id="err" class="error" hidden></div>
    <textarea id="input" rows="3" placeholder="Ask AIForge to change or explain something…"></textarea>
    <div class="bar">
      <select id="mode" title="How the next message runs">
        <option value="simple">Agent</option><option value="plan">Ask (read-only)</option>
        <option value="team">Team</option>
      </select>
      <span class="spacer"></span>
      <button id="new" class="link" data-action="newChat" title="Start a new chat">New chat</button>
      <button id="stop" hidden>Stop</button>
      <button id="send">Send</button>
    </div>
  </div>`;

const log = document.getElementById('log')!;
const input = document.getElementById('input') as HTMLTextAreaElement;
const modeSel = document.getElementById('mode') as HTMLSelectElement;
const sendBtn = document.getElementById('send') as HTMLButtonElement;
const stopBtn = document.getElementById('stop') as HTMLButtonElement;
const errBox = document.getElementById('err')!;

let frame = 0;
function render(): void {
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    const empty = !state.messages.length && !state.live && !state.pendingUser;
    const banners = (state.scratch
      ? '<div class="banner">This chat works in a scratch folder inside the AIForge sandbox, '
        + 'not in your open folder — its edits, diffs and "go back" apply there.</div>' : '')
      + (state.replaceFrom
        ? '<div class="banner">The folder is back to before that message. Your next message '
          + 'replaces it and everything after it (shown faded).</div>' : '');
    log.innerHTML = banners + (empty
      ? '<div class="empty">Ask for a change, a fix or an explanation. Changed files show up here with their diffs, and every message can be undone.</div>'
      : renderHistory(state.messages, { cwd: state.cwd, replaceFrom: state.replaceFrom,
                                        running: state.running })
        + renderLive(state.live, state.pendingUser, state.cwd) + renderApproval(state.approval));
    if (nearBottom) log.scrollTop = log.scrollHeight;
    stopBtn.hidden = !state.running;
    sendBtn.textContent = state.running ? 'Steer' : 'Send';
    input.placeholder = state.running
      ? 'Add guidance to the running task…' : 'Ask AIForge to change or explain something…';
    errBox.hidden = !state.error && !state.notice;
    errBox.className = state.error ? 'error' : 'notice';
    errBox.textContent = state.error || state.notice;
  });
}

function newTurn(): LiveTurn {
  return { role: 'assistant', text: '', steps: [], streaming: true };
}

/** The user's words back in the box (never lost when they did not go). */
function giveBack(text: string | null): void {
  if (text && !input.value.trim()) input.value = text;
}

function onEvent(ev: any): void {
  if (ev.type === 'ping' || ev.type === 'usage' || ev.type === 'suggestion') return;
  if (ev.type === 'approval') { state.approval = ev; render(); return; }
  // Only the end of the step answers an approval — another agent's tool call
  // (team mode) does not.
  if (ev.type === 'approval_expired' || ev.type === 'done'
      || (ev.type === 'message' && !ev.supplementary)) state.approval = null;
  if (ev.type === 'attached') {
    // A replay of a run in flight: start its turn fresh (the replay resends it all).
    if (ev.running) state.live = newTurn();
    render();
    return;
  }
  state.live = reduceTurn(state.live ?? newTurn(), ev, () => undefined);
  render();
}

window.addEventListener('message', e => {
  const m = e.data || {};
  switch (m.type) {
    case 'config': state.mode = m.mode || 'simple'; modeSel.value = state.mode; break;
    case 'session': state.cwd = String(m.cwd || ''); state.scratch = !!m.scratch; break;
    case 'replacing': state.replaceFrom = m.from ? Number(m.from) : null; break;
    case 'notice':
      state.notice = String(m.text || '');
      if (m.restoreText) { giveBack(state.pendingUser); state.pendingUser = null; }
      break;
    case 'history':
      state.messages = Array.isArray(m.messages) ? m.messages : [];
      if (!state.running) { state.live = null; state.pendingUser = null; }
      break;
    case 'event': onEvent(m.ev); return;
    case 'running':
      state.running = !!m.value;
      if (!state.running && state.live) state.live = { ...state.live, streaming: false };
      if (!state.running) state.approval = null;
      break;
    case 'echo': state.pendingUser = String(m.text || ''); state.live = newTurn(); break;
    case 'error': {
      state.error = String(m.text || '');
      if (typeof m.restoreText === 'string') giveBack(m.restoreText);     // a steer that did not go
      // A turn that never started (nothing streamed yet): hand the words back.
      const started = state.live && (state.live.steps.length || state.live.text || state.live.streamText);
      if (state.pendingUser && !started) {
        giveBack(state.pendingUser);
        state.pendingUser = null;
        state.live = null;
      }
      break;
    }
    case 'reset':
      Object.assign(state, { messages: [], live: null, pendingUser: null, approval: null, error: '' });
      break;
  }
  render();
});

function send(): void {
  const text = input.value.trim();
  if (!text) return;
  state.error = '';
  state.notice = '';
  if (!state.running) {
    state.pendingUser = text;
    state.live = newTurn();
  } else {
    state.live = reduceTurn(state.live ?? newTurn(),
      { type: 'thought', role: 'system', text: `↳ you: ${text}` }, () => undefined);
  }
  input.value = '';
  vscode.postMessage({ type: 'send', text });
  render();
}

sendBtn.addEventListener('click', send);
stopBtn.addEventListener('click', () => vscode.postMessage({ type: 'stop' }));
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
modeSel.addEventListener('change', () => vscode.postMessage({ type: 'mode', mode: modeSel.value }));

// The files (with their diffs) one reply changed — looked up here rather than
// copied into every button.
function filesOf(turn: number): FileChange[] {
  const m = state.messages.find(x => x.id === turn);
  return m ? turnChanges(m.steps || [], state.cwd) : [];
}

// One listener for every button in the log (rows are re-rendered freely).
app.addEventListener('click', e => {
  const el = (e.target as HTMLElement).closest('[data-action]') as HTMLElement | null;
  if (!el) return;
  const d = el.dataset;
  switch (d.action) {
    case 'openFile': {
      const f = d.turn ? filesOf(Number(d.turn)).find(x => x.path === d.path) : undefined;
      vscode.postMessage({ type: 'openFile', path: d.path, sha: d.sha, patch: f?.diff });
      break;
    }
    case 'explain': {
      if (state.running) break;
      const files = filesOf(Number(d.turn)).filter(f => !d.path || f.path === d.path);
      if (files.length) vscode.postMessage({ type: 'explain', files });
      break;
    }
    case 'undoFile': vscode.postMessage({ type: 'restore', sha: d.sha, paths: [d.path] }); break;
    case 'restore': vscode.postMessage({ type: 'restore', sha: d.sha, msgId: Number(d.msg) }); break;
    case 'approve':
    case 'reject':
      vscode.postMessage({ type: 'approve', id: Number(d.id), decision: d.action });
      state.approval = null;
      render();
      break;
    case 'newChat': vscode.postMessage({ type: 'newChat' }); break;
  }
});

render();
vscode.postMessage({ type: 'ready' });
