// AIForge for VS Code — a thin client over the AIForge API (the same one the
// web UI and the `aiforge` CLI use). The agent runs in the AIForge sandbox; the
// CLI starts that box and owns folder mounts, so this extension never grants
// the box access to anything by itself.
import * as vscode from 'vscode';
import { ChangesView, openDiff, showPatch } from './changesView';
import { ChatView } from './chatView';
import { Controller } from './controller';

export function activate(ctx: vscode.ExtensionContext): { controller: Controller } {
  const ctl = new Controller(ctx);
  const tree = new ChangesView(ctl);
  const run = (fn: () => unknown) => async () => {
    try {
      await fn();
    } catch (e) {
      void vscode.window.showErrorMessage(`AIForge: ${(e as Error).message}`);
    }
  };
  ctx.subscriptions.push(
    ctl,
    vscode.window.registerWebviewViewProvider(ChatView.id, new ChatView(ctx, ctl),
      { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.window.registerTreeDataProvider('aiforge.changes', tree),
    vscode.commands.registerCommand('aiforge.newChat', run(() => ctl.newChat())),
    vscode.commands.registerCommand('aiforge.stop', run(() => ctl.stop())),
    vscode.commands.registerCommand('aiforge.startBox', run(() => ctl.startBox())),
    vscode.commands.registerCommand('aiforge.mountWorkspace', run(() => ctl.mountWorkspace())),
    vscode.commands.registerCommand('aiforge.setToken', run(() => ctl.setToken())),
    vscode.commands.registerCommand('aiforge.clearChanges', run(() => {
      ctl.changeSet?.clear();
      ctl.changes.fire();
    })),
    vscode.commands.registerCommand('aiforge.openDiff',
      (p: string, sha?: string) => run(() => openDiff(ctl, p, sha))()),
    vscode.commands.registerCommand('aiforge.showPatch',
      (item: { hostPath: string } | string) =>
        run(() => showPatch(ctl, typeof item === 'string' ? item : item.hostPath))()),
  );
  return { controller: ctl };     // for the end-to-end test (e2e/suite.ts)
}

export function deactivate(): void {
  // Subscriptions dispose the controller; a live run keeps going server-side.
}
