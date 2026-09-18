// The chat webview: keeps the chat state, applies run events with the SAME
// reducer as the web UI (web/src/views/Chat.reduce.ts), and posts the user's
// actions to the extension. Rendering is batched to one frame so a stream of
// deltas does not rebuild the log per token.
import type { LiveTurn } from '../../../web/src/views/Chat.model';
import { reduceTurn } from '../../../web/src/views/Chat.reduce';
import { STYLE } from './style';
import { Msg, renderApproval, renderHistory, renderLive } from './view';

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
    log.innerHTML = empty
      ? '<div class="empty">Ask for a change, a fix or an explanation. Changed files show up here with their diffs, and every message can be undone.</div>'
      : renderHistory(state.messages, state.cwd) + renderLive(state.live, state.pendingUser, state.cwd)
        + renderApproval(state.approval);
    if (nearBottom) log.scrollTop = log.scrollHeight;
    stopBtn.hidden = !state.running;
    sendBtn.textContent = state.running ? 'Steer' : 'Send';
    input.placeholder = state.running
      ? 'Add guidance to the running task…' : 'Ask AIForge to change or explain something…';
    errBox.hidden = !state.error;
    errBox.textContent = state.error;
  });
}

function newTurn(): LiveTurn {
  return { role: 'assistant', text: '', steps: [], streaming: true };
}

function onEvent(ev: any): void {
  if (ev.type === 'ping' || ev.type === 'usage' || ev.type === 'suggestion') return;
  if (ev.type === 'approval') { state.approval = ev; render(); return; }
  if (ev.type === 'tool' || ev.type === 'message' || ev.type === 'approval_expired') state.approval = null;
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
    case 'session': state.cwd = String(m.cwd || ''); break;
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
    case 'error':
      state.error = String(m.text || '');
      if (!state.running && state.pendingUser) {
        // It never ran: hand the words back instead of losing them.
        if (!input.value.trim()) input.value = state.pendingUser;
        state.pendingUser = null;
        state.live = null;
      }
      break;
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

// One listener for every button in the log (rows are re-rendered freely).
app.addEventListener('click', e => {
  const el = (e.target as HTMLElement).closest('[data-action]') as HTMLElement | null;
  if (!el) return;
  const d = el.dataset;
  switch (d.action) {
    case 'openFile': vscode.postMessage({ type: 'openFile', path: d.path, sha: d.sha }); break;
    case 'explain': vscode.postMessage({ type: 'explain', path: d.path }); break;
    case 'undoFile': vscode.postMessage({ type: 'restore', sha: d.sha, paths: [d.path] }); break;
    case 'restore': vscode.postMessage({ type: 'restore', sha: d.sha }); break;
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
