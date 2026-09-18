// The chat panel: a webview in the AIForge sidebar. The page renders; this
// side relays its actions (send, stop, approve, open a diff) to the controller
// and the controller's events back to it.
import * as vscode from 'vscode';
import { Controller } from './controller';

export class ChatView implements vscode.WebviewViewProvider {
  static readonly id = 'aiforge.chat';
  private view: vscode.WebviewView | null = null;
  private queue: Record<string, unknown>[] = [];
  private ready = false;

  constructor(private readonly ctx: vscode.ExtensionContext, private readonly ctl: Controller) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    this.ready = false;
    const dist = vscode.Uri.joinPath(this.ctx.extensionUri, 'dist');
    view.webview.options = { enableScripts: true, localResourceRoots: [dist] };
    view.webview.html = page(view.webview, vscode.Uri.joinPath(dist, 'webview.js'));
    this.ctl.attachChat({ post: m => this.post(m) });
    view.onDidDispose(() => {
      this.view = null;
      this.ctl.attachChat(null);
    });
    view.webview.onDidReceiveMessage(m => this.onMessage(m).catch(e =>
      this.post({ type: 'error', text: (e as Error).message })));
  }

  private post(msg: Record<string, unknown>): void {
    // Messages sent before the page's script runs are lost: hold them.
    if (!this.view || !this.ready) {
      this.queue.push(msg);
      return;
    }
    void this.view.webview.postMessage(msg);
  }

  private async onMessage(m: any): Promise<void> {
    switch (m?.type) {
      case 'ready': {
        this.ready = true;
        this.ctl.viewReady = true;
        this.post({ type: 'config', mode: this.ctl.mode });
        for (const q of this.queue.splice(0)) this.post(q);
        await this.ctl.reopen();
        return;
      }
      case 'send': return this.ctl.send(String(m.text || ''));
      case 'stop': return this.ctl.stop();
      case 'newChat': return this.ctl.newChat();
      case 'approve': return this.ctl.approve(Number(m.id), m.decision === 'approve' ? 'approve' : 'reject');
      case 'restore':
        return this.ctl.restore(String(m.sha), Array.isArray(m.paths) ? m.paths.map(String) : undefined,
                                m.msgId ? Number(m.msgId) : undefined);
      case 'explain':
        return this.ctl.explain((Array.isArray(m.files) ? m.files : [])
          .map((f: any) => ({ path: String(f.path), diff: f.diff ? String(f.diff) : undefined })));
      case 'mode': return this.ctl.setMode(String(m.mode));
      case 'openFile':
        // No checkpoint for that turn (team mode): the agent's own patch is
        // the only honest "before ↔ after".
        if (!m.sha && m.patch) {
          const doc = await vscode.workspace.openTextDocument({ content: String(m.patch), language: 'diff' });
          await vscode.window.showTextDocument(doc, { preview: true });
          return;
        }
        await vscode.commands.executeCommand('aiforge.openDiff',
          this.ctl.hostPathOf(String(m.path)), m.sha ? String(m.sha) : undefined);
        return;
    }
  }
}

function page(webview: vscode.Webview, script: vscode.Uri): string {
  const nonce = [...Array(32)].map(() => Math.floor(Math.random() * 36).toString(36)).join('');
  // Nothing loads from the network; model text is rendered escaped (webview/md.ts).
  const csp = [`default-src 'none'`, `style-src 'unsafe-inline' ${webview.cspSource}`,
               `script-src 'nonce-${nonce}'`, `img-src ${webview.cspSource} data:`].join('; ');
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><div id="app"></div>
<script nonce="${nonce}" src="${webview.asWebviewUri(script)}"></script></body></html>`;
}
