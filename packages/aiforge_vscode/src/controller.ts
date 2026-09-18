// One controller per window: which AIForge chat belongs to this workspace,
// whether the sandbox is up and can see the folder, and where every event of a
// run goes (the chat view, the changed-files tree, approval prompts).
import * as path from 'node:path';
import * as vscode from 'vscode';
import { Api, ApiDown, Message, Session } from './api';
import { ChangeSet } from './changeset';
import { coveringMount, toBox, toHost } from './paths';
import { NotSteered, Runner } from './runner';
import { AgentEvent } from './sse';

const SESSION_KEY = 'aiforge.session';
const MODE_KEY = 'aiforge.mode';
const TOKEN_KEY = 'aiforge.apiToken';
// An "explain" prompt carries the turn's diff; more than this is cut.
const EXPLAIN_DIFF_MAX = 12_000;

export interface ChatSink {
  post(msg: Record<string, unknown>): void;
}

export type ExplainFile = { path: string; diff?: string };

export class Controller implements vscode.Disposable {
  readonly api: Api;
  readonly changes = new vscode.EventEmitter<void>();
  changeSet: ChangeSet | null = null;
  /** The folder as it was before this chat's first turn: the left side of the
   *  changed-files diffs ("what did the agent change in this chat"). */
  baseSha: string | null = null;
  /** The chat page has loaded and said so (its first message). */
  viewReady = false;
  private session: Session | null = null;
  private runner: Runner | null = null;
  private chat: ChatSink | null = null;
  private status: vscode.StatusBarItem;
  private pendingApproval: number | null = null;
  private prompted = new Set<number>();
  // After "go back": the next message REPLACES this user message and every
  // message after it (the server truncates the chat there).
  private replaceFrom: number | null = null;
  // One session setup at a time: reopening the stored chat and a first send
  // raced, and whichever bound last won.
  private settingUp: Promise<Session | null> | null = null;
  private warnedPlainToken = false;

  constructor(private readonly ctx: vscode.ExtensionContext) {
    this.api = new Api(() => config().get<string>('apiUrl', 'http://127.0.0.1:8799'),
                       async () => ctx.secrets.get(TOKEN_KEY));
    this.status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
    this.status.command = 'aiforge.chat.focus';
    this.setRunning(false);
    this.status.show();
  }

  dispose(): void {
    this.runner?.dispose();
    this.status.dispose();
    this.changes.dispose();
  }

  attachChat(chat: ChatSink | null): void { this.chat = chat; }

  get mode(): string {
    return this.ctx.workspaceState.get<string>(MODE_KEY) ?? config().get<string>('mode', 'simple');
  }

  /** Kept in this window's state, not in .vscode/settings.json (that dirtied
   *  the user's repo, and failed with no folder open). */
  async setMode(mode: string): Promise<void> {
    await this.ctx.workspaceState.update(MODE_KEY, mode);
  }

  // ── entry points (commands and the chat view) ────────────────────────────

  /** The chat view (re)opened: show this workspace's chat and any live run. */
  async reopen(): Promise<void> {
    await this.setUp(false);
  }

  async send(text: string): Promise<void> {
    if (this.runner?.running) {
      try {
        await this.runner.send(text, { mode: this.mode, reviewEdits: false });
      } catch (e) {
        this.post({ type: 'error', text: (e as Error).message, restoreText: text });
        if (!(e instanceof NotSteered)) throw e;
      }
      return;
    }
    const editFrom = this.replaceFrom ?? undefined;
    const ok = await this.run(text, { mode: this.mode, editFrom });
    if (ok && editFrom) {
      this.replaceFrom = null;
      this.post({ type: 'replacing', from: null });
    }
  }

  async stop(): Promise<void> { await this.runner?.stop(); }

  async newChat(): Promise<void> {
    if (this.runner?.running) {
      const go = await vscode.window.showWarningMessage(
        'A run is in progress. Start a new chat anyway? The run keeps going on the server.',
        'New chat');
      if (go !== 'New chat') return;
    }
    this.runner?.dispose();
    this.runner = null;
    this.session = null;
    this.baseSha = null;
    this.replaceFrom = null;
    await this.ctx.workspaceState.update(SESSION_KEY, undefined);
    this.changeSet?.clear();
    this.changes.fire();
    this.post({ type: 'reset' });
  }

  /** Go back to how the folder was before a turn — the whole folder (``msgId``
   *  = that user message: the next message then replaces it and everything
   *  after it, as in Cursor), or only ``paths`` (one file's "Undo"). A
   *  snapshot is taken first, so going back can itself be undone. */
  async restore(sha: string, paths?: string[], msgId?: number): Promise<void> {
    if (!this.session) return;
    if (this.runner?.running) {
      void vscode.window.showWarningMessage('Stop the run before going back to an earlier version.');
      return;
    }
    const what = paths?.length ? paths.join(', ') : 'the folder';
    const ok = await vscode.window.showWarningMessage(
      paths?.length
        ? `Put ${what} back the way it was before that message?`
        : 'Put the folder back the way it was before that message? Your next message '
          + 'will replace it and everything after it.',
      { modal: true, detail: 'A snapshot of the folder is taken first, so you can undo this.' },
      'Go back');
    if (ok !== 'Go back') return;
    const id = this.session.id;
    const before = await this.api.snapshot(id, `before going back to ${sha.slice(0, 8)}`)
      .catch(() => ({ ok: false } as { ok: boolean; sha?: string }));
    const r = await this.api.restore(id, sha, paths);
    if (!r.ok) throw new Error(`could not go back: ${r.error ?? 'unknown error'}`);
    if (!paths?.length && msgId) {
      this.replaceFrom = msgId;
      this.post({ type: 'replacing', from: msgId });
    }
    const pick = await vscode.window.showInformationMessage(
      paths?.length ? `Restored ${what}.` : 'The folder is back to that point.',
      ...(before.ok && before.sha ? ['Undo this'] : []));
    if (pick === 'Undo this' && before.sha) {
      const redo = await this.api.restore(id, before.sha);
      if (!redo.ok) throw new Error(`could not undo: ${redo.error ?? 'unknown error'}`);
      if (!paths?.length) {
        this.replaceFrom = null;
        this.post({ type: 'replacing', from: null });
      }
      void vscode.window.showInformationMessage('Back to where you were.');
    }
  }

  /** Explain a change in plain words. The turn's diff goes WITH the question,
   *  so the answer is about that turn even after later ones, and the agent has
   *  no reason to touch a file (review-edits is on for this message: any write
   *  would still wait for your Approve). */
  async explain(files: ExplainFile[]): Promise<void> {
    if (this.runner?.running) {
      void vscode.window.showInformationMessage(
        'Wait for the current run to finish (or Stop it), then ask for the explanation.');
      return;
    }
    const one = files.length === 1 ? files[0] : null;
    let diffs = files.filter(f => f.diff).map(f => `--- ${f.path}\n${f.diff}`).join('\n\n');
    if (diffs.length > EXPLAIN_DIFF_MAX) diffs = diffs.slice(0, EXPLAIN_DIFF_MAX) + '\n… (cut)';
    const ask = (one
      ? `In simple English, explain what changed in \`${one.path}\` and why. `
        + 'Two to four short sentences, no jargon.'
      : `In simple English, explain these changes (${files.map(f => `\`${f.path}\``).join(', ')}): `
        + 'for each file, what changed and why, in one or two short sentences. No jargon.')
      + ' Only explain — do not change any files.'
      + (diffs ? `\n\nThe change:\n\`\`\`diff\n${diffs}\n\`\`\`` : '');
    this.post({ type: 'echo', text: one ? `Explain the change to ${one.path}` : 'Explain these changes' });
    await this.run(ask, { mode: 'simple', quick: true, reviewEdits: true });
  }

  async approve(approvalId: number, decision: 'approve' | 'reject'): Promise<void> {
    if (!this.session || this.pendingApproval !== approvalId) return;
    this.pendingApproval = null;
    await this.api.approve(this.session.id, approvalId, decision);
  }

  async setToken(): Promise<void> {
    const t = await vscode.window.showInputBox({
      prompt: 'AIForge API token (AIFORGE_API_TOKEN). Leave empty to clear.',
      password: true, ignoreFocusOut: true,
    });
    if (t === undefined) return;
    if (t) await this.ctx.secrets.store(TOKEN_KEY, t);
    else await this.ctx.secrets.delete(TOKEN_KEY);
  }

  /** Start the sandbox in a visible terminal — the CLI owns the box. */
  startBox(): void { this.cli(['box', 'up']); }

  /** Ask the CLI to mount this folder. It is the HOST side of the mount gate:
   *  the box can only request a folder; the host approves it. */
  mountWorkspace(): void {
    const folder = workspaceFolder();
    if (folder) this.cli(['mount', 'add', folder]);
  }

  /** A path the agent reported, as a file on this machine. */
  hostPathOf(reported: string): string {
    return this.changeSet ? this.changeSet.hostPathOf(reported) : reported;
  }

  // ── internals ────────────────────────────────────────────────────────────

  /** Start a turn (never a steer). False when it could not start. */
  private async run(text: string, opts: { mode: string; quick?: boolean; reviewEdits?: boolean;
                                          editFrom?: number }): Promise<boolean> {
    if (!(await this.ready())) {
      this.post({ type: 'error', text: 'AIForge is not reachable — your message was not sent.' });
      return false;
    }
    const session = await this.setUp(true);
    if (!session || !this.runner) {
      this.post({ type: 'error', text: 'No chat was opened — your message was not sent.' });
      return false;
    }
    this.warnPlainToken();
    await this.runner.send(text, {
      mode: opts.mode, quick: opts.quick, editFrom: opts.editFrom,
      reviewEdits: opts.reviewEdits ?? config().get<boolean>('reviewEdits', false),
    });
    return true;
  }

  /** Bind this window to its chat: the stored one when it still exists, else
   *  (``create``) a new one. Serialised. */
  private setUp(create: boolean): Promise<Session | null> {
    if (this.session) return Promise.resolve(this.session);
    if (!this.settingUp) {
      this.settingUp = this.doSetUp(create).finally(() => { this.settingUp = null; });
      return this.settingUp;
    }
    return this.settingUp.then(s => s ?? (create ? this.setUp(true) : null));
  }

  private async doSetUp(create: boolean): Promise<Session | null> {
    const id = this.ctx.workspaceState.get<number>(SESSION_KEY);
    if (id && (await this.api.healthy())) {
      try {
        const { session, messages } = await this.api.session(id);
        this.bind(session);
        this.showHistory(messages);
        void this.runner!.attach();
        return session;
      } catch (e) {
        // Only a chat the server no longer has is forgotten — a 401, a 5xx or
        // a timeout must not cost the user their conversation.
        if (!(e instanceof ApiDown && /^404\b/.test(e.message))) {
          this.post({ type: 'error', text: `Could not load this chat: ${(e as Error).message}` });
          return null;
        }
        await this.ctx.workspaceState.update(SESSION_KEY, undefined);
      }
    }
    return create ? this.openSession() : null;
  }

  private async ready(): Promise<boolean> {
    if (await this.api.healthy()) return true;
    const local = isLoopback(config().get<string>('apiUrl', ''));
    const pick = await vscode.window.showWarningMessage(
      local ? 'The AIForge sandbox is not running.' :
        `AIForge is not answering at ${config().get('apiUrl')}.`,
      ...(local ? ['Start it'] : []), 'Settings');
    if (pick === 'Start it') this.startBox();
    if (pick === 'Settings') {
      void vscode.commands.executeCommand('workbench.action.openSettings', 'aiforge');
    }
    return false;
  }

  /** A new chat for this workspace, working in the folder when the box can see
   *  it, else (the user's choice) in a scratch workspace inside the box. */
  private async openSession(): Promise<Session | null> {
    const folder = workspaceFolder();
    let boxCwd: string | null = null;
    if (folder && isLoopback(config().get<string>('apiUrl', ''))) {
      boxCwd = toBox(folder);
      if (!(await this.boxSees(boxCwd))) {
        const pick = await vscode.window.showWarningMessage(
          `The agent can't see ${folder} yet. Mount it (you approve it on this machine; `
          + 'the sandbox restarts), or chat in a scratch workspace inside the sandbox.',
          'Mount this folder', 'Use a scratch workspace');
        if (pick === 'Mount this folder') {
          this.mountWorkspace();
          this.post({ type: 'error', text: 'Mounting the folder — send your message again once '
            + 'the sandbox has restarted (see the AIForge terminal).' });
          return null;
        }
        if (pick !== 'Use a scratch workspace') return null;
        boxCwd = null;
      }
    }
    try {
      const session = await this.api.createSession(boxCwd);
      await this.ctx.workspaceState.update(SESSION_KEY, session.id);
      this.bind(session);
      return session;
    } catch (e) {
      this.error(e);
      return null;
    }
  }

  private async boxSees(boxPath: string): Promise<boolean> {
    try {
      const m = await this.api.mounts();
      if (m.sandbox === false) return true;       // native mode: the host FS itself
      const live = m.folders.filter(f => f.status === 'mounted').map(f => f.path);
      return coveringMount(boxPath, live) !== null;
    } catch {
      return true;          // an older API without the route: let the run decide
    }
  }

  private bind(session: Session): void {
    this.runner?.dispose();
    this.session = session;
    this.prompted.clear();
    const folder = workspaceFolder();
    const boxCwd = session.cwd || (folder ? toBox(folder) : '/');
    const hostCwd = session.cwd ? toHost(session.cwd) : (folder ?? boxCwd);
    this.changeSet = new ChangeSet(boxCwd, hostCwd);
    this.changes.fire();
    // Working somewhere other than the open folder (a scratch workspace in the
    // sandbox): say so, or edits and "go back" seem to do nothing.
    const rel = folder ? path.relative(folder, hostCwd) : '..';
    const scratch = rel.startsWith('..') || path.isAbsolute(rel);
    this.post({ type: 'session', cwd: boxCwd, scratch });
    // Callbacks from a runner that has been replaced (the view was re-created,
    // a new chat) are dropped: its late "stopped" flipped the live one to idle.
    const runner: Runner = new Runner(this.api, session.id, {
      onEvent: ev => { if (this.runner === runner) this.onEvent(ev); },
      onRunning: running => {
        if (this.runner !== runner) return;
        this.setRunning(running);
        if (!running) void this.reloadHistory(session.id);
      },
      onError: message => { if (this.runner === runner) this.post({ type: 'error', text: message }); },
      onNotice: message => {
        if (this.runner === runner) this.post({ type: 'notice', text: message, restoreText: true });
      },
    });
    this.runner = runner;
  }

  /** After a run: the persisted turn (with its checkpoint, for "go back")
   *  replaces the live one, as the web UI does. */
  private async reloadHistory(id: number): Promise<void> {
    if (this.session?.id !== id) return;
    try {
      const { messages } = await this.api.session(id);
      // A new run may have started meanwhile: its live turn must stay.
      if (this.session?.id === id && !this.runner?.running) this.showHistory(messages);
    } catch {
      // keep the live view; the next open reloads it
    }
  }

  private showHistory(messages: Message[]): void {
    this.baseSha = messages.find(m => m.role === 'user' && m.checkpoint_sha)?.checkpoint_sha ?? null;
    // The changed-files tree covers the whole chat, not just this window's runs.
    if (this.changeSet) {
      this.changeSet.clear();
      for (const m of messages) {
        for (const s of m.role === 'assistant' ? m.steps ?? [] : []) {
          this.changeSet.apply({ ...s, type: s.type ?? s.kind });
        }
      }
      this.changes.fire();
    }
    this.post({ type: 'history', messages });
  }

  private onEvent(ev: AgentEvent): void {
    this.post({ type: 'event', ev });
    if (this.changeSet?.apply(ev)) this.changes.fire();
    if (ev.type === 'approval') void this.promptApproval(ev);
    // Other agents' tool calls (team mode) do not answer an open approval.
    if (ev.type === 'approval_expired' || ev.type === 'done'
        || (ev.type === 'message' && !ev.supplementary)) {
      this.pendingApproval = null;
    }
    if (ev.type === 'message' && ev.awaiting_input) {
      void vscode.window.showInformationMessage('AIForge is asking you something.', 'Answer')
        .then(p => { if (p) void vscode.commands.executeCommand('aiforge.chat.focus'); });
    }
  }

  /** The chat view shows an Approve/Reject card; a notification makes sure a
   *  blocked run is noticed when the view is closed. First answer wins. */
  private async promptApproval(ev: AgentEvent): Promise<void> {
    const id = Number(ev.id);
    this.pendingApproval = id;
    if (this.prompted.has(id)) return;           // a re-attach replays old approvals
    this.prompted.add(id);
    for (;;) {
      const pick = await vscode.window.showWarningMessage(
        `AIForge wants to run ${ev.name}${ev.reason ? ` — ${ev.reason}` : ''}`,
        'Approve', 'Reject', 'Details');
      if (pick === 'Details') {
        const doc = await vscode.workspace.openTextDocument({
          content: String(ev.preview || JSON.stringify(ev.args, null, 2)), language: 'markdown' });
        await vscode.window.showTextDocument(doc, { preview: true });
        if (this.pendingApproval !== id) return;
        continue;                                // ask again after the details
      }
      if (pick === 'Approve' || pick === 'Reject') {
        if (this.pendingApproval !== id) {
          void vscode.window.showInformationMessage('That approval was already answered.');
          return;
        }
        await this.approve(id, pick === 'Approve' ? 'approve' : 'reject').catch(e => this.error(e));
      }
      return;
    }
  }

  private setRunning(running: boolean): void {
    void vscode.commands.executeCommand('setContext', 'aiforge.running', running);
    this.status.text = running ? '$(sync~spin) AIForge' : '$(tools) AIForge';
    this.status.tooltip = running ? 'AIForge is working — open the chat' : 'Open the AIForge chat';
    this.post({ type: 'running', value: running });
  }

  /** Run the CLI in a visible terminal with no shell in between, so nothing
   *  needs quoting (PowerShell, cmd and bash all quote differently). */
  private cli(args: string[]): void {
    const term = vscode.window.createTerminal({
      name: 'AIForge', shellPath: config().get<string>('cliPath', 'aiforge'), shellArgs: args,
    });
    term.show();
  }

  private warnPlainToken(): void {
    const url = config().get<string>('apiUrl', '');
    if (this.warnedPlainToken || isLoopback(url) || !url.startsWith('http://')) return;
    void this.ctx.secrets.get(TOKEN_KEY).then(t => {
      if (!t || this.warnedPlainToken) return;
      this.warnedPlainToken = true;
      void vscode.window.showWarningMessage(
        'The AIForge API token is sent over plain http. Use an https URL or an SSH tunnel.');
    });
  }

  private post(msg: Record<string, unknown>): void { this.chat?.post(msg); }

  private error(e: unknown): void {
    const text = e instanceof Error ? e.message : String(e);
    this.post({ type: 'error', text });
    void vscode.window.showErrorMessage(`AIForge: ${text}`);
  }
}

export const config = () => vscode.workspace.getConfiguration('aiforge');

export function workspaceFolder(): string | undefined {
  const active = vscode.window.activeTextEditor?.document.uri;
  const f = (active && vscode.workspace.getWorkspaceFolder(active))
    ?? vscode.workspace.workspaceFolders?.[0];
  return f?.uri.scheme === 'file' ? f.uri.fsPath : undefined;
}

export function isLoopback(url: string): boolean {
  try {
    const h = new URL(url).hostname;
    return h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '[::1]';
  } catch {
    return false;
  }
}
