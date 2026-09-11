// Server-sent events WITH the API token.
//
// The browser's EventSource cannot send an Authorization header, so every live
// stream (logs, traces, the workflow graph) 401'd for a UI opened from another
// host — the one case that needs the token. This is the same small interface
// (onopen / onmessage / onerror / close) over fetch, which can send it.
// Reconnects after a dropped connection like EventSource does; an HTTP error
// (401, 404, 500) is final, also like EventSource.
import { authHeaders } from './core';

const RETRY_MS = 3000;

export class AuthedEventSource {
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: ((err?: unknown) => void) | null = null;
  private ctrl: AbortController | null = null;
  private closed = false;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly url: string) {
    // Start after the caller has assigned its handlers (same tick semantics
    // as EventSource, whose events never fire synchronously).
    queueMicrotask(() => { void this.connect(); });
  }

  close(): void {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.ctrl?.abort();
  }

  private async connect(): Promise<void> {
    if (this.closed) return;
    this.ctrl = new AbortController();
    let res: Response;
    try {
      res = await fetch(this.url, {
        headers: { Accept: 'text/event-stream', ...authHeaders() },
        signal: this.ctrl.signal,
        cache: 'no-store',
      });
    } catch (e) {
      return this.dropped(e);
    }
    if (!res.ok || !res.body) {
      this.closed = true;                  // final, like EventSource on non-200
      this.onerror?.(new Error(`${res.status} ${res.statusText}`));
      return;
    }
    this.onopen?.();
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true }).replace(/\r\n?/g, '\n');
        let cut = buf.indexOf('\n\n');
        while (cut >= 0) {
          this.dispatch(buf.slice(0, cut));
          buf = buf.slice(cut + 2);
          cut = buf.indexOf('\n\n');
        }
      }
    } catch (e) {
      return this.dropped(e);
    }
    this.dropped();
  }

  private dispatch(block: string): void {
    const data: string[] = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('data:')) data.push(line.slice(line.startsWith('data: ') ? 6 : 5));
    }
    if (data.length) this.onmessage?.({ data: data.join('\n') });
  }

  private dropped(err?: unknown): void {
    if (this.closed) return;
    this.onerror?.(err);
    this.timer = setTimeout(() => { void this.connect(); }, RETRY_MS);
  }
}
