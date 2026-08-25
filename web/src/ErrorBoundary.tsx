import React from 'react';

/* A render-error firewall. Chat renders arbitrary model output; a single
 * malformed payload that throws during render would otherwise unmount the whole
 * SPA to a blank page (Suspense only catches lazy-load promises, not render
 * throws). This catches it and shows a recoverable fallback instead. */
interface State { error: Error | null; info: string; copied: boolean; }

export class ErrorBoundary extends React.Component<
  { children: React.ReactNode; fallback?: (e: Error, reset: () => void) => React.ReactNode },
  State
> {
  state: State = { error: null, info: '', copied: false };
  static getDerivedStateFromError(error: Error) { return { error }; }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('render error:', error, info.componentStack);
    // Keep WHICH component threw, not just what it said. "Cannot read
    // properties of undefined (reading 'length')" is the same message from
    // twenty different views; without the component name the report a user
    // sends is unactionable, and they are the only one who can reproduce it.
    this.setState({ info: (info?.componentStack || '').trim() });
  }

  /** The first few frames of the component stack — the innermost is the one
   *  that threw. Trimmed because the full stack is the entire app shell. */
  private where(): string {
    return this.state.info.split('\n').map(l => l.trim())
      .filter(Boolean).slice(0, 4).join('\n');
  }

  private readonly copy = () => {
    const text = [
      `path: ${typeof location !== 'undefined' ? location.pathname : '?'}`,
      `error: ${String(this.state.error?.message || this.state.error)}`,
      this.state.error?.stack || '',
      '--- component stack ---',
      this.state.info,
    ].join('\n');
    // navigator.clipboard does not exist in an insecure context, and the API
    // is documented as reachable over plain HTTP from another host — so the
    // common case here is no clipboard at all. Fall back to the textarea
    // trick, and SAY which happened: a button that silently does nothing
    // reads as broken, on a screen the user already distrusts.
    const done = () => this.setState({ copied: true });
    try {
      const p = navigator.clipboard?.writeText(text);
      // `!= null`, not truthiness: `p` is a Promise (always truthy) OR undefined
      // when clipboard is unavailable, so the test is about EXISTENCE, and
      // truthiness on a Promise reads as if its resolved value were checked.
      if (p != null) { p.then(done).catch(() => this.legacyCopy(text) && done()); }
      else if (this.legacyCopy(text)) done();
    } catch { if (this.legacyCopy(text)) done(); }
  };

  private legacyCopy(text: string): boolean {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch { return false; }
  }

  reset = () => this.setState({ error: null, info: '', copied: false });

  render() {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback(this.state.error, this.reset);
      return (
        <div style={{ padding: 24, maxWidth: 640, margin: '40px auto', color: 'var(--fg-1)' }}>
          <h2 style={{ marginBottom: 8 }}>Something broke rendering this view.</h2>
          <div className="small muted" style={{ marginBottom: 12 }}>
            The rest of the app is fine — this is contained to the current view.
          </div>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: 'var(--fg-3)',
                        background: 'var(--bg-1)', padding: 10, borderRadius: 8,
                        border: '1px solid var(--border-1)', marginBottom: 12 }}>
            {String(this.state.error?.message || this.state.error)}
            {this.where() ? `\n\nin:\n${this.where()}` : ''}
          </pre>
          <div className="row" style={{ gap: 8 }}>
            <button type="button" className="btn" onClick={this.reset}>Try again</button>
            <button type="button" className="ghost" onClick={this.copy}
                    title="Copy the error, the path and the component stack">
              {this.state.copied ? '✓ Copied' : '⧉ Copy details'}
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
