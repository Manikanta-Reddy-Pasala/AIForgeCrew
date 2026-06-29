import React from 'react';

/* A render-error firewall. Chat renders arbitrary model output; a single
 * malformed payload that throws during render would otherwise unmount the whole
 * SPA to a blank page (Suspense only catches lazy-load promises, not render
 * throws). This catches it and shows a recoverable fallback instead. */
interface State { error: Error | null; }

export class ErrorBoundary extends React.Component<
  { children: React.ReactNode; fallback?: (e: Error, reset: () => void) => React.ReactNode },
  State
> {
  state: State = { error: null };
  static getDerivedStateFromError(error: Error): State { return { error }; }
  componentDidCatch(error: Error) { console.error('render error:', error); }
  reset = () => this.setState({ error: null });

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
          </pre>
          <button className="btn" onClick={this.reset}>Try again</button>
        </div>
      );
    }
    return this.props.children;
  }
}
