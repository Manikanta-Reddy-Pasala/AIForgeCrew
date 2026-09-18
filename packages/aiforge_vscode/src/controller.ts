// One controller per window: which AIForge chat belongs to this workspace,
// whether the sandbox is up and can see the folder, and where every event of a
// run goes (the chat view, the changed-files tree, approval prompts).
import * as vscode from 'vscode';
import { Api, ApiDown, Message, Session } from './api';
import { ChangeSet } from './changeset';
import { coveringMount, toBox, toHost } from './paths';
import { Runner } from './runner';
import { AgentEvent } from './sse';

const SESSION_KEY = 'aiforge.session';
const TOKEN_KEY = 'aiforge.apiToken';

export interface ChatSink {
  post(msg: Record<string, unknown>): void;
}

export class Controller implements vscode.Disposable {
  readonly api: Api;
  readonly changes = new vscode.EventEmitter<void>();
  changeSet: ChangeSet | null = null;
  private session: Session | null = null;
  private runner: Runner | null = null;
  private chat: ChatSink | null = null;
  private status: vscode.StatusBarItem;
  private pendingApproval: number | null = null;

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

  // ── entry points (commands and the chat view) ────────────────────────────

  /** The chat view (re)opened: show this workspace's chat and any live run. */
  async reopen(): Promise<void> {
    const id = this.ctx.workspaceState.get<number>(SESSION_KEY);
    if (!id || !(await this.api.healthy())) return;
    try {
      const { session, messages } = await this.api.session(id);
      this.bind(session);
      this.noteHistory(messages);
      this.post({ type: 'history', messages });
      await this.runner!.attach();
    } catch {
      await this.ctx.workspaceState.update(SESSION_KEY, undefined);   // deleted
    }
  }

  async send(text: string): Promise<void> {
    await this.sendAs(text, config().get<string>('mode', 'simple'));
  }

  private async sendAs(text: string, mode: string): Promise<void> {
    if (!(await this.ready())) {
      this.post({ type: 'error', text: 'AIForge is not reachable — your message was not sent.' });
      return;
    }
    const session = this.session ?? (await this.openSession());
    if (!session) {
      this.post({ type: 'error', text: 'No chat was opened — your message was not sent.' });
      return;
    }
    await this.runner!.send(text, {
      mode, reviewEdits: config().get<boolean>('reviewEdits', false),
    });
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
    await this.ctx.workspaceState.update(SESSION_KEY, undefined);
    this.changeSet?.clear();
    this.changes.fire();
    this.post({ type: 'reset' });
  }

  /** Go back to how the folder was before a turn — the whole folder, or only
   *  ``paths`` (one file's "Undo"). Asks first: this overwrites files. */
  async restore(sha: string, paths?: string[]): Promise<void> {
    if (!this.session) return;
    if (this.runner?.running) {
      void vscode.window.showWarningMessage('Stop the run before going back to an earlier version.');
      return;
    }
    const what = paths?.length ? paths.join(', ') : 'every file in this chat\'s folder';
    const ok = await vscode.window.showWarningMessage(
      `Put ${what} back the way it was before that message? Changes made since are lost.`,
      { modal: true }, 'Go back');
    if (ok !== 'Go back') return;
    const r = await this.api.restore(this.session.id, sha, paths);
    if (!r.ok) throw new Error(`could not go back: ${r.error ?? 'unknown error'}`);
    void vscode.window.showInformationMessage(
      paths?.length ? `Restored ${what}.` : 'The folder is back to that point.');
  }

  /** Ask the agent to explain its changes in plain words — in read-only plan
   *  mode, so explaining can never edit anything. */
  async explain(file?: string): Promise<void> {
    const ask = file
      ? `In simple English, explain what you changed in \`${file}\` and why. `
        + 'Two to four short sentences, no jargon.'
      : 'In simple English, explain the changes you just made: for each file, '
        + 'what changed and why, in one or two short sentences. No jargon.';
    this.post({ type: 'echo', text: ask });
    await this.sendAs(ask, 'plan');
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

  // ── internals ────────────────────────────────────────────────────────────

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
        if (pick === 'Mount this folder') { this.mountWorkspace(); return null; }
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
    // The chat's own folder, not whichever editor is active now.
    const folder = workspaceFolder();
    const boxCwd = session.cwd || (folder ? toBox(folder) : '/');
    this.changeSet = new ChangeSet(boxCwd, session.cwd ? toHost(session.cwd) : (folder ?? boxCwd));
    this.post({ type: 'session', cwd: boxCwd });
    this.changes.fire();
    this.runner = new Runner(this.api, session.id, {
      onEvent: ev => this.onEvent(ev),
      onRunning: running => {
        this.setRunning(running);
        if (!running) void this.reloadHistory(session.id);
      },
      onError: message => this.post({ type: 'error', text: message }),
    });
  }

  /** After a run: the persisted turn (with its checkpoint, for "go back")
   *  replaces the live one, as the web UI does. */
  private async reloadHistory(id: number): Promise<void> {
    if (this.session?.id !== id) return;
    try {
      const { messages } = await this.api.session(id);
      if (this.session?.id !== id) return;
      this.noteHistory(messages);
      this.post({ type: 'history', messages });
    } catch {
      // keep the live view; the next open reloads it
    }
  }

  /** The folder as it was before this chat's first turn: the left side of the
   *  changed-files diffs ("what did the agent change in this chat"). */
  baseSha: string | null = null;

  private noteHistory(messages: Message[]): void {
    this.baseSha = messages.find(m => m.role === 'user' && m.checkpoint_sha)?.checkpoint_sha ?? null;
  }

  /** A path the agent reported, as a file on this machine. */
  hostPathOf(reported: string): string {
    return this.changeSet ? this.changeSet.hostPathOf(reported) : reported;
  }

  private onEvent(ev: AgentEvent): void {
    this.post({ type: 'event', ev });
    if (this.changeSet?.apply(ev)) this.changes.fire();
    if (ev.type === 'approval') void this.promptApproval(ev);
    if (ev.type === 'tool' || ev.type === 'message' || ev.type === 'approval_expired') {
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
    const pick = await vscode.window.showWarningMessage(
      `AIForge wants to run ${ev.name}${ev.reason ? ` — ${ev.reason}` : ''}`,
      'Approve', 'Reject', 'Details');
    if (pick === 'Details') {
      const doc = await vscode.workspace.openTextDocument({
        content: String(ev.preview || JSON.stringify(ev.args, null, 2)), language: 'markdown' });
      await vscode.window.showTextDocument(doc, { preview: true });
      return;
    }
    if (pick === 'Approve' || pick === 'Reject') {
      await this.approve(id, pick === 'Approve' ? 'approve' : 'reject').catch(e => this.error(e));
    }
  }

  private setRunning(running: boolean): void {
    void vscode.commands.executeCommand('setContext', 'aiforge.running', running);
    this.status.text = running ? '$(sync~spin) AIForge' : '$(tools) AIForge';
    this.status.tooltip = running ? 'AIForge is working — open the chat' : 'Open the AIForge chat';
    this.post({ type: 'running', value: running });
  }

  private cli(args: string[]): void {
    const term = vscode.window.createTerminal({ name: 'AIForge' });
    term.show();
    const quote = (a: string) => (/^[\w./:@=-]+$/.test(a) ? a : `"${a.replace(/(["\\$`])/g, '\\$1')}"`);
    term.sendText([config().get<string>('cliPath', 'aiforge'), ...args].map(quote).join(' '));
  }

  private post(msg: Record<string, unknown>): void { this.chat?.post(msg); }

  private error(e: unknown): void {
    const text = e instanceof ApiDown || e instanceof Error ? e.message : String(e);
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
