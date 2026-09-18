// Pure pieces: SSE framing, host↔box paths, the changed-files set.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ChangeSet } from '../src/changeset';
import { coveringMount, reportedToHost, toBox, toHost, within } from '../src/paths';
import { SseParser, parseSseLine } from '../src/sse';

test('sse: events split across chunks and CRLF framing', () => {
  const p = new SseParser();
  assert.deepEqual(p.feed('data: {"type":"del'), []);
  assert.deepEqual(p.feed('ta","text":"a"}\r\n\r\ndata: {"type":"done"}\n'),
    [{ type: 'delta', text: 'a' }, { type: 'done' }]);
  assert.deepEqual(p.feed('data: {"type":"ping"}'), []);
  assert.deepEqual(p.flush(), [{ type: 'ping' }]);
});

test('sse: comments, blank data and non-JSON never end a run', () => {
  assert.equal(parseSseLine(': keepalive'), null);
  assert.equal(parseSseLine('data:'), null);
  assert.equal(parseSseLine('data: not json'), null);
  assert.equal(parseSseLine('data: [1,2]'), null);
});

test('paths: windows drive ↔ /host, others unchanged', () => {
  assert.equal(toBox('C:\\work\\app', 'win32'), '/host/c/work/app');
  assert.equal(toHost('/host/c/work/app', 'win32'), 'C:\\work\\app');
  assert.equal(toBox('/Users/me/app', 'darwin'), '/Users/me/app');
  assert.equal(toHost('/srv/app', 'linux'), '/srv/app');
});

test('paths: macOS compares case-insensitively, linux does not', () => {
  assert.ok(within('/Users/Me/App/src', '/users/me/app', 'darwin'));
  assert.ok(!within('/Users/Me/App', '/users/me/app', 'linux'));
  assert.ok(!within('/work/apple', '/work/app', 'linux'));          // not a prefix match
});

test('paths: the longest covering mount wins', () => {
  assert.equal(coveringMount('/w/a/b', ['/w', '/w/a', '/x'], 'linux'), '/w/a');
  assert.equal(coveringMount('/y', ['/w'], 'linux'), null);
});

test('paths: reported paths resolve to this machine', () => {
  assert.equal(reportedToHost('src/a.ts', '/w/app', '/w/app', 'linux'), '/w/app/src/a.ts');
  assert.equal(reportedToHost('/w/app/src/a.ts', '/w/app', '/w/app', 'linux'), '/w/app/src/a.ts');
  assert.equal(reportedToHost('/host/c/w/app/a.ts', '/host/c/w/app', 'C:\\w\\app', 'win32'),
    'C:\\w\\app\\a.ts');
});

test('changes: tool writes appear at once; a changes event adds diffs', () => {
  const cs = new ChangeSet('/w', '/w', 'linux');
  assert.ok(cs.apply({ type: 'tool', name: 'file_write', args: { path: 'a.ts' }, result: { ok: true } }));
  assert.ok(!cs.apply({ type: 'tool', name: 'file_write', args: { path: 'b.ts' }, result: { ok: false } }));
  assert.ok(!cs.apply({ type: 'tool', name: 'editor', args: { command: 'view', path: 'c.ts' }, result: {} }));
  assert.ok(!cs.apply({ type: 'tool', name: 'file_read', args: { path: 'd.ts' }, result: {} }));
  assert.deepEqual(cs.list().map(f => [f.label, f.status]), [['a.ts', 'written']]);
  cs.apply({ type: 'changes', files: [{ path: 'a.ts', status: 'modified', additions: 2, deletions: 1, diff: '+x' }] });
  const [a] = cs.list();
  assert.equal(a.status, 'modified');
  assert.equal(a.patch, '+x');
  assert.equal(a.hostPath, '/w/a.ts');
});

test('changes: the code-graph index is not the user\'s work', () => {
  const cs = new ChangeSet('/w', '/w', 'linux');
  cs.apply({ type: 'changes', files: [{ path: '.codegraph/.gitignore', status: 'added' },
                                      { path: 'a.ts', status: 'modified' }] });
  assert.deepEqual(cs.list().map(f => f.label), ['a.ts']);
});
