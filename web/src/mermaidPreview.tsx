/* Render a finished ```mermaid fence as an SVG beside its source.
 *
 * Loaded on demand so a chat with no diagram does not pay for the mermaid
 * bundle. securityLevel strict: the source is model-written, and a diagram
 * cannot turn HTML or click handlers on. Renders are queued — mermaid keeps
 * one global config, and overlapping draws clobber each other.
 */
import React from 'react';

type MermaidApi = {
  initialize: (config: {
    startOnLoad: boolean;
    securityLevel: 'strict';
    theme: 'neutral';
    suppressErrorRendering: boolean;
  }) => void;
  render: (id: string, text: string) => Promise<{ svg: string }>;
};

let apiPromise: Promise<MermaidApi> | null = null;
let renderChain: Promise<unknown> = Promise.resolve();

function mermaidApi(): Promise<MermaidApi> {
  apiPromise ??= import('mermaid').then((mod) => {
    const api = mod.default as MermaidApi;
    api.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'neutral',
      suppressErrorRendering: true,
    });
    return api;
  });
  return apiPromise;
}

function renderQueued(api: MermaidApi, id: string, source: string) {
  const run = renderChain.then(() => api.render(id, source));
  renderChain = run.then(() => undefined, () => undefined);
  return run;
}

let seq = 0;

function dropRenderNodes(id: string) {
  document.getElementById(id)?.remove();
  document.getElementById(`d${id}`)?.remove();
}

export function MermaidPreview({ source }: Readonly<{ source: string }>) {
  const host = React.useRef<HTMLDivElement>(null);
  const [state, setState] = React.useState<'loading' | 'ok' | 'error'>('loading');
  const [message, setMessage] = React.useState('');

  React.useEffect(() => {
    const id = `mmd${++seq}`;
    let cancelled = false;
    if (host.current) host.current.replaceChildren();
    setState('loading');
    setMessage('');
    mermaidApi()
      .then((api) => renderQueued(api, id, source))
      .then(({ svg }) => {
        if (cancelled || !host.current) {
          dropRenderNodes(id);
          return;
        }
        const doc = new DOMParser().parseFromString(svg, 'image/svg+xml');
        const root = doc.documentElement;
        if (root.nodeName.toLowerCase() !== 'svg') {
          throw new Error('Diagram preview was not an SVG');
        }
        host.current.replaceChildren(document.importNode(root, true));
        setState('ok');
      })
      .catch((err: unknown) => {
        dropRenderNodes(id);
        if (host.current) host.current.replaceChildren();
        if (cancelled) return;
        const raw = err instanceof Error ? err.message : 'Could not draw this diagram';
        setMessage(raw.replace(/\s+/g, ' ').slice(0, 240));
        setState('error');
      });
    return () => { cancelled = true; };
  }, [source]);

  return (
    <div className="mermaid-preview" aria-label="Diagram preview">
      {state === 'loading' && <div className="mermaid-preview-status">Drawing diagram…</div>}
      {state === 'error' && (
        <div className="mermaid-preview-status mermaid-preview-error">{message}</div>
      )}
      <div ref={host} className="mermaid-preview-svg" />
    </div>
  );
}
