// The API client against a fake fetch, and the run driver's reconnect rules.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Api, ApiDown, Busy, Stalled } from '../src/api';
import { MAX_REATTACH, NotSteered, Runner } from '../src/runner';

function sseResponse(chunks: string[], { hang = false } = {}): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(ctrl) {
      for (const c of chunks) ctrl.enqueue(enc.encode(c));
      if (!hang) ctrl.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

const api = (fetchFn: any, stallMs = 50) =>
  new Api(() => 'http://127.0.0.1:8799', async () => 'tok', { fetchFn, stallMs });

async function collect(gen: AsyncGenerator<any>): Promise<any[]> {
  const out = [];
  for await (const e of gen) out.push(e);
  return out;
}

test('api: a stream yields events and sends the token', async () => {
  let auth = '';
  const a = api(async (_u: string, init: any) => {
    auth = init.headers.Authorization;
    return sseResponse(['data: {"type":"delta","text":"hi"}\n\n', 'data: {"type":"done"}\n\n']);
  });
  assert.deepEqual(await collect(a.send(1, 'x', { mode: 'simple', reviewEdits: false })),
    [{ type: 'delta', text: 'hi' }, { type: 'done' }]);
  assert.equal(auth, 'Bearer tok');
});

test('api: silence past the stall budget is Stalled, not a hang', async () => {
  const a = api(async (_u: string, init: any) => {
    const r = sseResponse(['data: {"type":"ping"}\n\n'], { hang: true });
    init.signal.addEventListener('abort', () => undefined);
    return r;
  });
  await assert.rejects(collect(a.attach(1)), Stalled);
});

test('api: 409 is Busy, 401 names the token command', async () => {
  const a = api(async () => new Response('{"detail":"running"}', { status: 409 }));
  await assert.rejects(collect(a.send(1, 'x', { mode: 'simple', reviewEdits: false })), Busy);
  const b = api(async () => new Response('', { status: 401 }));
  await assert.rejects(b.sessions(), (e: Error) => e instanceof ApiDown && /Set API token/.test(e.message));
});

test('api: restore asks the server to remove files created since', async () => {
  let body: any = null;
  const a = api(async (_u: string, init: any) => {
    body = JSON.parse(init.body);
    return new Response('{"ok":true}', { status: 200 });
  });
  await a.restore(3, 'abc123', ['src/a.ts']);
  assert.deepEqual(body, { sha: 'abc123', paths: ['src/a.ts'], delete_orphans: true });
});

// A scripted API: each send/attach call returns the next scripted stream.
function fakeApi(script: Array<() => AsyncGenerator<any>>) {
  const calls: string[] = [];
  const next = (kind: string) => { calls.push(kind); return script.shift()!(); };
  return {
    calls,
    send: () => next('send'),
    attach: () => next('attach'),
    stop: async () => { calls.push('stop'); },
    steer: async (): Promise<any> => { calls.push('steer'); return { queued: true }; },
  };
}

async function* events(evs: any[], fail?: Error) {
  for (const e of evs) yield e;
  if (fail) throw fail;
}

test('runner: a dropped stream re-attaches and finishes the run', async () => {
  const f = fakeApi([
    () => events([{ type: 'delta', text: 'a' }], new Stalled('drop')),
    () => events([{ type: 'attached', running: true }, { type: 'done' }]),
  ]);
  const seen: any[] = [];
  const states: boolean[] = [];
  const r = new Runner(f as any, 1, { onEvent: e => seen.push(e.type),
    onRunning: v => states.push(v), onError: m => assert.fail(m), onNotice: () => undefined });
  await r.send('x', { mode: 'simple', reviewEdits: false });
  assert.deepEqual(f.calls, ['send', 'attach']);
  assert.deepEqual(seen, ['delta', 'attached', 'done']);
  assert.deepEqual(states, [true, false]);
});

test('runner: a busy chat is watched, and the user is told their message did not go', async () => {
  const f = fakeApi([() => events([], new Busy('409')), () => events([{ type: 'done' }])]);
  const notices: string[] = [];
  const r = new Runner(f as any, 1, { onEvent: () => undefined, onRunning: () => undefined,
    onError: m => assert.fail(m), onNotice: m => notices.push(m) });
  await r.send('x', { mode: 'simple', reviewEdits: false });
  assert.match(notices[0], /not sent/);
  assert.deepEqual(f.calls, ['send', 'attach']);
});

test('runner: the drop budget is for drops in a row, not per run', async () => {
  const drop = () => events([{ type: 'delta', text: '.' }], new Stalled('drop'));
  const script = Array.from({ length: MAX_REATTACH + 2 }, () => drop);
  script.push(() => events([{ type: 'done' }]));
  const f = fakeApi(script);
  const r = new Runner(f as any, 1, { onEvent: () => undefined, onRunning: () => undefined,
    onError: m => assert.fail(`gave up: ${m}`), onNotice: () => undefined });
  await r.send('x', { mode: 'simple', reviewEdits: false });   // each drop made progress
});

test('runner: a steer the server did not take is reported, not swallowed', async () => {
  let release!: () => void;
  const gate = new Promise<void>(res => { release = res; });
  const f: any = fakeApi([async function* () { await gate; yield { type: 'done' }; }]);
  f.steer = async () => ({ queued: false });
  const r = new Runner(f, 1, { onEvent: () => undefined, onRunning: () => undefined,
    onError: m => assert.fail(m), onNotice: () => undefined });
  const run = r.send('first', { mode: 'simple', reviewEdits: false });
  await assert.rejects(r.send('late', { mode: 'simple', reviewEdits: false }), NotSteered);
  release();
  await run;
});

test('runner: it gives up after MAX_REATTACH drops and says so', async () => {
  const drops = Array.from({ length: MAX_REATTACH + 1 }, () => () => events([], new Stalled('drop')));
  const f = fakeApi(drops);
  let error = '';
  const r = new Runner(f as any, 1, { onEvent: () => undefined, onRunning: () => undefined,
    onError: m => { error = m; }, onNotice: () => undefined });
  await r.send('x', { mode: 'simple', reviewEdits: false });
  assert.equal(f.calls.length, MAX_REATTACH + 1);
  assert.match(error, /drop/);
});

test('runner: attach with nothing running ends quietly; a send while running steers', async () => {
  const f = fakeApi([() => events([{ type: 'attached', running: false }, { type: 'done' }])]);
  const seen: any[] = [];
  const r = new Runner(f as any, 1, { onEvent: e => seen.push(e), onRunning: () => undefined,
    onError: m => assert.fail(m), onNotice: () => undefined });
  await r.attach();
  assert.deepEqual(seen, []);
  let release!: () => void;
  const gate = new Promise<void>(res => { release = res; });
  f.send = (async function* () { await gate; yield { type: 'done' }; }) as any;
  const run = r.send('first', { mode: 'simple', reviewEdits: false });
  await r.send('more', { mode: 'simple', reviewEdits: false });
  release();
  await run;
  assert.ok(f.calls.includes('steer'));
});
