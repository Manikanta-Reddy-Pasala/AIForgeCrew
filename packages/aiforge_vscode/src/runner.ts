// Drives one chat session's runs: send, re-attach after a dropped stream, stop,
// steer. Knows nothing about VS Code — events go to ``onEvent`` — so the
// reconnect rules are testable against a fake API.
import { Api, Busy, SendOptions, Stalled } from './api';
import { AgentEvent } from './sse';

// A flaky connection resumes; a box that keeps dropping us is reported.
export const MAX_REATTACH = 3;

export type RunHooks = {
  onEvent(ev: AgentEvent): void;
  onRunning(running: boolean): void;
  onError(message: string): void;
  /** Something the user must know that is not an event of the run (their
   *  message did not go because another client is running this chat). */
  onNotice(message: string): void;
};

/** A steer the server did not take: the run had just ended, or cannot steer. */
export class NotSteered extends Error {}

// After Stop, the server ends the stream itself (it saves the stopped turn
// first); wait this long before dropping it anyway.
export const STOP_GRACE_MS = 15_000;

export class Runner {
  private ctrl: AbortController | null = null;
  private _running = false;

  constructor(private readonly api: Api, private readonly sessionId: number,
              private readonly hooks: RunHooks) {}

  get running(): boolean { return this._running; }

  /** Start a turn — or, while one runs, steer it (the text is folded in at the
   *  agent's next step, as the web UI does). */
  async send(content: string, opts: SendOptions): Promise<void> {
    if (this._running) {
      const r = await this.api.steer(this.sessionId, content);
      if (!r?.queued) {
        throw new NotSteered(r?.unsupported
          ? 'This run cannot take guidance — wait for it to finish, then send it.'
          : 'The run had already finished — send it again as a new message.');
      }
      return;
    }
    await this.drive(signal => this.api.send(this.sessionId, content, opts, signal));
  }

  /** Pick up a run already in flight (a reload, the web UI, the CLI). No-op
   *  when nothing runs. */
  async attach(): Promise<void> {
    if (this._running) return;
    await this.drive(signal => this.api.attach(this.sessionId, signal));
  }

  async stop(): Promise<void> {
    // Stop the SERVER's run and let its stream end by itself: the server saves
    // the stopped turn and says so. Dropping the stream at once made the
    // stopped turn vanish from the chat. Only a stream that never ends is cut.
    const ctrl = this.ctrl;
    await this.api.stop(this.sessionId);
    if (ctrl) setTimeout(() => { if (this.ctrl === ctrl) ctrl.abort(); }, STOP_GRACE_MS);
  }

  dispose(): void { this.ctrl?.abort(); }

  private async drive(open: (s: AbortSignal) => AsyncGenerator<AgentEvent>): Promise<void> {
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    this.setRunning(true);
    let stream = open(ctrl.signal);
    let reattached = 0;
    try {
      for (;;) {
        try {
          for await (const ev of stream) {
            if (ev.type === 'attached' && !ev.running) return;   // nothing in flight
            reattached = 0;             // the budget is for drops IN A ROW
            this.hooks.onEvent(ev);
          }
          return;                                   // the run ended its stream
        } catch (e) {
          if (ctrl.signal.aborted) return;
          const retry = e instanceof Busy || (e instanceof Stalled && reattached < MAX_REATTACH);
          if (!retry) throw e;
          // 409: someone else runs this chat — watch theirs. Stalled: the run
          // is still alive server-side, so resume it rather than report an error.
          if (e instanceof Stalled) reattached += 1;
          else this.hooks.onNotice('This chat is already running elsewhere (the web '
            + 'UI or the CLI), so your message was not sent — showing that run.');
          stream = this.api.attach(this.sessionId, ctrl.signal);
        }
      }
    } catch (e) {
      this.hooks.onError((e as Error).message);
    } finally {
      if (this.ctrl === ctrl) this.ctrl = null;
      this.setRunning(false);
    }
  }

  private setRunning(v: boolean): void {
    if (this._running === v) return;
    this._running = v;
    this.hooks.onRunning(v);
  }
}
