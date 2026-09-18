// The end-to-end flow a user goes through, inside a real VS Code:
// open the chat → ask for a fix → approve the edit → the file changes →
// the changed-files view lists it → Diff opens a diff tab → Explain answers
// without editing → Undo puts the file back.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

async function until(what: string, ok: () => boolean, ms = 30_000): Promise<void> {
  const end = Date.now() + ms;
  while (!ok()) {
    if (Date.now() > end) throw new Error(`timed out waiting for: ${what}`);
    await sleep(200);
  }
}

function step(name: string) { console.log(`[e2e] ${name}`); }

export async function run(): Promise<void> {
  const ws = vscode.workspace.workspaceFolders![0].uri.fsPath;
  const file = path.join(ws, 'calc.py');
  const original = readFileSync(file, 'utf8');
  const cfg = vscode.workspace.getConfiguration('aiforge');
  await cfg.update('apiUrl', process.env.AIFORGE_E2E_API, vscode.ConfigurationTarget.Global);
  await cfg.update('reviewEdits', true, vscode.ConfigurationTarget.Global);

  // Answer the prompts a user would click: approve the edit, confirm "go back".
  const prompts: string[] = [];
  (vscode.window as any).showWarningMessage = async (msg: string, ...items: any[]) => {
    prompts.push(msg);
    const labels = items.filter(i => typeof i === 'string');
    return labels.find(l => l === 'Approve' || l === 'Go back') ?? undefined;
  };
  let undoGoBack = false;
  (vscode.window as any).showInformationMessage = async (_m: string, ...items: any[]) =>
    (undoGoBack && items.includes('Undo this') ? 'Undo this' : undefined);

  step('activate');
  const ext = vscode.extensions.all.find(e => e.packageJSON.name === 'aiforge-vscode')!;
  const { controller: ctl } = await ext.activate();

  step('open the chat view (the webview loads and says ready)');
  await vscode.commands.executeCommand('aiforge.chat.focus');
  await until('chat webview ready', () => ctl.viewReady, 60_000);

  step('ask for a fix (review-edits on: the edit waits for approval)');
  await ctl.send('Fix the bug in calc.py: add() subtracts. Change only that line.');
  assert.ok(prompts.some(p => /wants to run/.test(p)), `no approval prompt; prompts: ${prompts}`);
  const fixed = readFileSync(file, 'utf8');
  assert.match(fixed, /a \+ b/, 'the file was not fixed');

  step('changed files lists calc.py; history has the checkpoint');
  await until('changed files', () => !!ctl.changeSet?.list().some((f: any) => f.hostPath === file));
  await until('checkpoint from history', () => !!ctl.baseSha, 15_000);

  step('Diff opens a before ↔ after tab');
  await vscode.commands.executeCommand('aiforge.openDiff', file);
  await until('diff tab', () => vscode.window.tabGroups.activeTabGroup.activeTab?.input
    instanceof vscode.TabInputTextDiff, 10_000);

  step('Explain in simple English (the file must not change)');
  const change = ctl.changeSet!.list().find((f: any) => f.hostPath === file)!;
  await ctl.explain([{ path: 'calc.py', diff: change.patch }]);
  const hist = await ctl.api.session((ctl as any).session.id);
  const answer = [...hist.messages].reverse().find((m: any) => m.role === 'assistant')?.content ?? '';
  console.log(`[e2e] explanation: ${answer.slice(0, 240)}`);
  assert.ok(answer.length > 20, 'no explanation');
  assert.equal(readFileSync(file, 'utf8'), fixed, 'explaining edited the file');

  step('Undo the file, then "Undo this" (the snapshot taken first brings the fix back)');
  undoGoBack = true;
  await ctl.restore(ctl.baseSha!, ['calc.py']);
  assert.equal(readFileSync(file, 'utf8'), fixed, '"Undo this" did not bring the fix back');

  step('Undo the file for real');
  undoGoBack = false;
  await ctl.restore(ctl.baseSha!, ['calc.py']);
  assert.equal(readFileSync(file, 'utf8'), original, 'undo did not restore the file');

  step('ALL PASSED');
}
