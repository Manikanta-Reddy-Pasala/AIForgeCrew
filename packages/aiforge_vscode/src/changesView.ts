// "Changed files": what the agent wrote this chat. A click opens VS Code's diff
// of the file against git HEAD — the working file IS the agent's result (the
// sandbox mounts this folder at the same path) — or, outside git, the agent's
// own patch.
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { ChangedFile } from './changeset';
import { Controller } from './controller';

export class ChangesView implements vscode.TreeDataProvider<ChangedFile> {
  private readonly emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;

  constructor(private readonly ctl: Controller) {
    ctl.changes.event(() => this.emitter.fire());
  }

  getChildren(el?: ChangedFile): ChangedFile[] {
    return el ? [] : (this.ctl.changeSet?.list() ?? []);
  }

  getTreeItem(f: ChangedFile): vscode.TreeItem {
    const item = new vscode.TreeItem(path.basename(f.hostPath));
    const counts = f.additions !== undefined ? ` +${f.additions} −${f.deletions ?? 0}` : '';
    item.description = `${path.dirname(f.label) === '.' ? '' : path.dirname(f.label)}${counts}`;
    item.tooltip = `${f.hostPath}\n${f.status}${counts}`;
    item.resourceUri = vscode.Uri.file(f.hostPath);
    item.contextValue = f.patch ? 'aiforge.file.patch' : 'aiforge.file';
    item.command = { command: 'aiforge.openDiff', title: 'Open diff', arguments: [f.hostPath] };
    return item;
  }
}

/** Diff the file as it was BEFORE (``sha``: the checkpoint AIForge took before
 *  that turn; default the chat's first one, else git HEAD) against the file
 *  now. Falls back to the agent's patch, then to the file itself. */
export async function openDiff(ctl: Controller, hostPath: string, sha?: string): Promise<void> {
  const f = ctl.changeSet?.list().find(x => x.hostPath === hostPath);
  const uri = vscode.Uri.file(hostPath);
  const git = await gitApi();
  const repo = git?.getRepository(uri);
  const exists = fs.existsSync(hostPath);
  const ref = sha || ctl.baseSha || 'HEAD';
  if (repo && f?.status !== 'added') {
    const before = git.toGitUri(uri, ref);
    const right = exists ? uri : vscode.Uri.parse('untitled:' + path.basename(hostPath) + ' (deleted)');
    const label = ref === 'HEAD' ? 'HEAD' : 'before';
    await vscode.commands.executeCommand('vscode.diff', before, right,
      `${path.basename(hostPath)} (${label} ↔ AIForge)`);
    return;
  }
  if (f?.patch && !exists) return showPatch(ctl, hostPath);
  if (exists) await vscode.window.showTextDocument(uri, { preview: true });
  else if (f?.patch) await showPatch(ctl, hostPath);
  else void vscode.window.showWarningMessage(`${hostPath} is not on this machine.`);
}

export async function showPatch(ctl: Controller, hostPath: string): Promise<void> {
  const f = ctl.changeSet?.list().find(x => x.hostPath === hostPath);
  if (!f?.patch) return;
  const doc = await vscode.workspace.openTextDocument({ content: f.patch, language: 'diff' });
  await vscode.window.showTextDocument(doc, { preview: true });
}

// The built-in Git extension's API (vscode.git), when it is enabled.
async function gitApi(): Promise<any | null> {
  const ext = vscode.extensions.getExtension('vscode.git');
  if (!ext) return null;
  try {
    const exports = ext.isActive ? ext.exports : await ext.activate();
    return exports?.getAPI?.(1) ?? null;
  } catch {
    return null;
  }
}
