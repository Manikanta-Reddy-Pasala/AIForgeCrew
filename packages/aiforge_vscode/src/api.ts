// The AIForge API as this extension sees it — the same routes the web UI and
// the CLI use, so nothing here needs server-side support. A token is sent when
// one is set (a non-loopback API requires AIFORGE_API_TOKEN); loopback needs
// none.
import { AgentEvent, SseParser } from './sse';

// A run can sit quiet through a long build, but the API pings every few
// seconds, so silence past this is a dead stream: the caller re-attaches.
export const STREAM_STALL_MS = 130_000;
// /api/health answers in 0.1 s idle and ~3 s while a run is in flight — a
// short whole-request budget reported a busy box as down (CLI lesson).
export const HEALTH_TIMEOUT_MS = 12_000;

export class ApiDown extends Error {}
/** A run is already in flight for this session (409): attach to it instead. */
export class Busy extends Error {}
/** The stream went silent past every keepalive. */
export class Stalled extends Error {}

export type Session = { id: number; title?: string; cwd?: string | null };
export type Message = { id: number; role: 'user' | 'assistant'; content: string; steps: any[];
                        checkpoint_sha?: string | null };
export type Mounts = { sandbox?: boolean; folders: { path: string; kind: string; status: string }[] };
export type SendOptions = { mode: string; reviewEdits: boolean };

type FetchFn = typeof fetch;

export class Api {
  private readonly fetchFn: FetchFn;
  private readonly stallMs: number;

  constructor(private readonly base: () => string,
              private readonly token: () => Promise<string | undefined>,
              opts: { fetchFn?: FetchFn; stallMs?: number } = {}) {
    this.fetchFn = opts.fetchFn ?? ((...a) => fetch(...a));
    this.stallMs = opts.stallMs ?? STREAM_STALL_MS;
  }

  // ── plain calls ──────────────────────────────────────────────────────────

  async healthy(): Promise<boolean> {
    try {
      const r = await this.fetchFn(this.url('/api/health'),
        { signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS) });
      return r.ok;
    } catch {
      return false;
    }
  }

  sessions(): Promise<Session[]> { return this.json('GET', '/api/chat/sessions'); }

  /** ``boxCwd`` null lets the API give the chat its own scratch workspace —
   *  the right answer when the folder is not mounted in the box. */
  createSession(boxCwd: string | null): Promise<Session> {
    return this.json('POST', '/api/chat/sessions', boxCwd ? { cwd: boxCwd } : {});
  }

  session(id: number): Promise<{ session: Session; messages: Message[] }> {
    return this.json('GET', `/api/chat/sessions/${id}`);
  }

  mounts(): Promise<Mounts> { return this.json('GET', '/api/runtime/mounts'); }
  stop(id: number): Promise<unknown> { return this.json('POST', `/api/chat/sessions/${id}/stop`); }

  // The server's field is `content` (the CLI once sent `text`: 422 every time).
  steer(id: number, content: string): Promise<unknown> {
    return this.json('POST', `/api/chat/sessions/${id}/steer`, { content });
  }

  approve(id: number, approvalId: number, decision: 'approve' | 'reject'): Promise<unknown> {
    return this.json('POST', `/api/chat/sessions/${id}/approve`, { id: approvalId, decision });
  }

  /** Put the chat's folder back to a checkpoint — the snapshot AIForge takes
   *  before every turn. ``paths`` limits it to those files; files the agent
   *  created after the snapshot are removed too, so "undo" really undoes. */
  restore(id: number, sha: string, paths?: string[]): Promise<{ ok: boolean; error?: string }> {
    return this.json('POST', `/api/chat/sessions/${id}/checkpoints/restore`,
      { sha, paths: paths?.length ? paths : null, delete_orphans: true });
  }

  // ── streams ──────────────────────────────────────────────────────────────

  send(id: number, content: string, opts: SendOptions, signal?: AbortSignal): AsyncGenerator<AgentEvent> {
    return this.stream('POST', `/api/chat/sessions/${id}/message`,
      { content, mode: opts.mode, review_edits: opts.reviewEdits }, signal);
  }

  /** Replay an in-flight run's buffered events, then tail it live. The first
   *  event says whether anything is running. */
  attach(id: number, signal?: AbortSignal): AsyncGenerator<AgentEvent> {
    return this.stream('GET', `/api/chat/sessions/${id}/attach`, undefined, signal);
  }

  // ── internals ────────────────────────────────────────────────────────────

  private url(p: string): string { return this.base().replace(/\/+$/, '') + p; }

  private async headers(json: boolean): Promise<Record<string, string>> {
    const h: Record<string, string> = json ? { 'Content-Type': 'application/json' } : {};
    const t = await this.token();
    if (t) h.Authorization = `Bearer ${t}`;
    return h;
  }

  private async json<T>(method: string, p: string, body?: unknown): Promise<T> {
    let r: Response;
    try {
      r = await this.fetchFn(this.url(p), {
        method, headers: await this.headers(body !== undefined),
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(20_000),
      });
    } catch (e) {
      throw new ApiDown(`AIForge is not answering at ${this.base()} (${(e as Error).message})`);
    }
    await raiseFor(r);
    const text = await r.text();
    return (text ? JSON.parse(text) : {}) as T;
  }

  private async *stream(method: string, p: string, body: unknown,
                        outer?: AbortSignal): AsyncGenerator<AgentEvent> {
    const ctrl = new AbortController();
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    let stalled = false;
    // Abort the request AND cancel the body: a body that ignores the signal
    // would otherwise keep read() pending forever.
    const cut = () => { ctrl.abort(); reader?.cancel().catch(() => undefined); };
    const onOuter = () => cut();
    outer?.addEventListener('abort', onOuter);
    let timer = setTimeout(() => { stalled = true; cut(); }, this.stallMs);
    const kick = () => {
      clearTimeout(timer);
      timer = setTimeout(() => { stalled = true; cut(); }, this.stallMs);
    };
    try {
      let r: Response;
      try {
        r = await this.fetchFn(this.url(p), {
          method, headers: { Accept: 'text/event-stream', ...(await this.headers(body !== undefined)) },
          body: body === undefined ? undefined : JSON.stringify(body), signal: ctrl.signal,
        });
      } catch (e) {
        if (outer?.aborted) return;
        if (stalled) throw new Stalled(`no answer for ${this.stallMs / 1000}s`);
        throw new ApiDown(`AIForge is not answering at ${this.base()} (${(e as Error).message})`);
      }
      await raiseFor(r);
      if (!r.body) throw new ApiDown('the API sent no stream');
      const parser = new SseParser();
      const dec = new TextDecoder();
      reader = r.body.getReader();
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          kick();
          for (const ev of parser.feed(dec.decode(value, { stream: true }))) yield ev;
        }
      } catch (e) {
        if (outer?.aborted) return;
        if (!stalled) throw new Stalled((e as Error).message);
      }
      if (outer?.aborted) return;
      if (stalled) throw new Stalled(`no events for ${this.stallMs / 1000}s`);
      for (const ev of parser.flush()) yield ev;
    } finally {
      clearTimeout(timer);
      outer?.removeEventListener('abort', onOuter);
      // A consumer that stops early (attach found nothing running) must not
      // leave the connection open.
      cut();
    }
  }
}

async function raiseFor(r: Response): Promise<void> {
  if (r.ok) return;
  const detail = await r.text().then(t => {
    try {
      const j = JSON.parse(t);
      return String(j.detail ?? j.error ?? t);
    } catch {
      return t;
    }
  }).catch(() => '');
  if (r.status === 409) throw new Busy(detail || 'a run is already in flight');
  if (r.status === 401) {
    throw new ApiDown('401 — this AIForge needs an API token: run "AIForge: Set API token"');
  }
  throw new ApiDown(`${r.status} ${detail}`.trim());
}
