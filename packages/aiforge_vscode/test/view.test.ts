// The webview's HTML: model text is escaped, changes cards carry the right
// checkpoint for "go back", and the shared reducer drives the live turn.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { reduceTurn } from '../../../web/src/views/Chat.reduce';
import { renderDiff } from '../webview/diff';
import { renderMarkdown } from '../webview/md';
import { renderHistory, renderLive, turnChanges } from '../webview/view';

test('markdown: raw HTML from the model never reaches the page', () => {
  const html = renderMarkdown('hi <script>alert(1)</script> <img src=x onerror=a()>');
  assert.ok(!html.includes('<script'));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('markdown: code, bold, lists and only http(s) links', () => {
  const html = renderMarkdown('**Done**: `x < y`\n\n- one\n- two\n\n[ok](https://a.io) [bad](javascript:alert(1))');
  assert.ok(html.includes('<strong>Done</strong>'));
  assert.ok(html.includes('<code>x &lt; y</code>'));
  assert.ok(html.includes('<ul><li>one</li><li>two</li></ul>'));
  assert.ok(html.includes('<a href="https://a.io">ok</a>'));
  assert.ok(!html.includes('javascript:alert(1)"'));
});

test('diff: rows are coloured and escaped', () => {
  const html = renderDiff('--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old <b>\n+new');
  assert.ok(html.includes('<div class="dl del">-old &lt;b&gt;</div>'));
  assert.ok(html.includes('<div class="dl add">+new</div>'));
  assert.ok(!html.includes('+++ b/x'));
});

test('a turn lists files from its changes step and from tool writes', () => {
  const files = turnChanges([
    { kind: 'tool', name: 'file_write', args: { path: 'b.ts' }, result: { ok: true } },
    { kind: 'changes', files: [{ path: 'a.ts', status: 'modified', additions: 1, deletions: 0, diff: '+a' }] },
  ]);
  assert.deepEqual(files.map(f => f.path).sort(), ['a.ts', 'b.ts']);
});

test('history: "go back" and file Undo use the checkpoint taken before that message', () => {
  const html = renderHistory([
    { id: 1, role: 'user', content: 'fix it', steps: [], checkpoint_sha: 'sha1' },
    { id: 2, role: 'assistant', content: 'Fixed.', steps: [
      { kind: 'changes', files: [{ path: 'a.ts', status: 'modified', additions: 1, deletions: 1, diff: '+a' }] }] },
  ]);
  assert.ok(html.includes('data-action="restore" data-sha="sha1"'));
  assert.ok(html.includes('data-action="undoFile" data-path="a.ts" data-sha="sha1"'));
  assert.ok(html.includes('Explain all in simple English'));
});

test('live: streamed text shows as it arrives and survives a gate note', () => {
  let t: any = { role: 'assistant', text: '', steps: [], streaming: true };
  for (const ev of [{ type: 'delta', phase: 'reset' }, { type: 'delta', phase: 'answer', text: 'Hello ' },
                    { type: 'delta', phase: 'answer', text: 'world' }]) {
    t = reduceTurn(t, ev, () => undefined);
  }
  assert.ok(renderLive(t, 'q').includes('Hello world'));
  t = reduceTurn(t, { type: 'thought', role: 'system', text: 'checking' }, () => undefined);
  assert.ok(renderLive(t, 'q').includes('Hello world'));        // kept as a step
});

test('markdown: inline code survives (it came back as digits once)', () => {
  const html = renderMarkdown('changed `a - b` to `a + b` in `calc.py`');
  assert.ok(html.includes('<code>a - b</code> to <code>a + b</code> in <code>calc.py</code>'));
});

test('one file named absolutely and relatively is one row', () => {
  const files = turnChanges([
    { kind: 'tool', name: 'file_patch', args: { path: '/w/app/calc.py' }, result: { ok: true } },
    { kind: 'changes', files: [{ path: 'calc.py', status: 'modified', additions: 1, deletions: 1 }] },
  ], '/w/app');
  assert.deepEqual(files.map(f => [f.path, f.status]), [['calc.py', 'modified']]);
});

test('a draft that is really a tool call is not shown', () => {
  const t: any = { role: 'assistant', text: '', steps: [], streaming: true,
                   draft: 'by running Python. ACTION: list_dir ARGS_JSON: {}' };
  assert.ok(!renderLive(t, null).includes('ACTION'));
  t.draft = 'THOUGHT: reading the tests first';
  assert.ok(renderLive(t, null).includes('reading the tests first'));
});

test('markdown: sub-points nest under their numbered item and numbering continues', () => {
  const html = renderMarkdown('1. **Naming:** confusing\n2. **Grammar:**\n   - a\n   - b\n3. Structure\n\n4. Missing:\n- x');
  assert.equal(html, '<ol><li><strong>Naming:</strong> confusing</li><li><strong>Grammar:</strong>'
    + '<ul><li>a</li><li>b</li></ul></li><li>Structure</li><li>Missing:<ul><li>x</li></ul></li></ol>');
});

test('after "go back" the replaced turns are faded and lose their buttons', () => {
  const msgs: any[] = [
    { id: 1, role: 'user', content: 'a', steps: [], checkpoint_sha: 's1' },
    { id: 2, role: 'assistant', content: 'A', steps: [{ kind: 'changes', files: [{ path: 'x', status: 'modified' }] }] },
    { id: 3, role: 'user', content: 'b', steps: [], checkpoint_sha: 's2' },
    { id: 4, role: 'assistant', content: 'B', steps: [{ kind: 'changes', files: [{ path: 'y', status: 'modified' }] }] },
  ];
  const html = renderHistory(msgs, { replaceFrom: 3 });
  assert.ok(html.includes('data-action="restore" data-sha="s1" data-msg="1"'));
  assert.ok(!/data-action="(restore|undoFile)"[^>]*data-sha="s2"/.test(html));
  assert.equal((html.match(/class="turn [a-z]+ replaced"/g) ?? []).length, 2);
});

test('while a run is going, past turns offer no Explain / Undo / go back', () => {
  const msgs: any[] = [
    { id: 1, role: 'user', content: 'a', steps: [], checkpoint_sha: 's1' },
    { id: 2, role: 'assistant', content: 'A', steps: [{ kind: 'changes', files: [{ path: 'x', status: 'modified' }] }] },
  ];
  const html = renderHistory(msgs, { running: true });
  assert.ok(!/data-action="(explain|undoFile|restore)"/.test(html));
  assert.ok(html.includes('data-action="openFile"'));
});
