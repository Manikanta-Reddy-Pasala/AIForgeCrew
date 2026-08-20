/**
 * Toolbar LLM request meter.
 *
 * "How many requests is AIForge actually making?" was unanswerable from the
 * UI: the chat footer counts one chat's turn, but the background daemon
 * (compaction folds, scope classification, jobs, retries) sends calls nobody
 * sees — and those are the ones that make an interactive turn crawl and a
 * shared endpoint rate-limit us.
 *
 * The pill shows the live rate; opening it shows the last minute / 15 minutes
 * / hour, a per-minute sparkline, and which ROLE and provider the hour's calls
 * went to. Counted at the wire on the server (retries included), in-process,
 * reset on API restart.
 */
import React, { useEffect, useRef, useState } from 'react';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { api } from '../api/client';
import type { LlmUsage } from '../api/agents';

/** Rate colour: neutral up to 20/min, amber to 60, red above — 60/min being
 *  the threshold the chat footer already flags. */
function rateTone(perMin: number): string {
  if (perMin > 60) return 'var(--err)';        // the chat footer's own threshold
  if (perMin > 20) return 'var(--warn)';
  return 'var(--fg-2)';
}

function Sparkline({ data }: { data: number[] }) {
  const max = Math.max(1, ...data);
  return (
    <div className="llm-meter-spark" aria-hidden="true">
      {data.map((v, i) => (
        <span
          key={i}
          style={{
            height: `${Math.max(v > 0 ? 2 : 1, Math.round((v / max) * 28))}px`,
            background: v > 0 ? 'var(--accent)' : 'var(--border-0)',
          }}
        />
      ))}
    </div>
  );
}

function Rows({ title, data }: { title: string; data: Record<string, number> }) {
  const rows = Object.entries(data || {}).sort((a, b) => b[1] - a[1]).slice(0, 8);
  if (!rows.length) return null;
  return (
    <div className="llm-meter-rows">
      <div className="llm-meter-label">{title}</div>
      {rows.map(([k, v]) => (
        <div key={k} className="llm-meter-row">
          <span>{k}</span><b>{v}</b>
        </div>
      ))}
    </div>
  );
}

function LlmMeterInner() {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement | null>(null);
  // The fetch reads the CURRENT open state without being keyed on it (see the
  // query below): the sparkline is only worth fetching while it is drawn.
  const openRef = useRef(open);
  openRef.current = open;

  // Poll faster while the panel is open (someone is watching a number move),
  // slowly while it is just a pill. The series is only fetched when it is
  // actually drawn.
  // ONE cache entry, whether the panel is open or shut. Keying on `open`
  // switched to an empty entry on every toggle, so the pill dropped to
  // "0/min · 0 1h" for a round trip — the meter reporting an idle system at
  // the exact moment someone opened it to look.
  const q = useQuery({
    queryKey: ['llm-usage'],
    queryFn: () => api.llmUsage(openRef.current),
    refetchInterval: open ? 3000 : 15000,
    placeholderData: keepPreviousData,
    staleTime: 0,
  });

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const u: LlmUsage | undefined = q.data;
  // An unreachable API must not read as a quiet one — but it must not read as
  // a LIVE one either. react-query keeps the last good `data` across a failing
  // refetch, so "no data" never becomes true after the first success: kill the
  // API and the pill would freeze on a stale "45/min" with the tooltip still
  // calling it the last minute. Key on the error and on how old the numbers
  // are, and stop showing them once they cannot be current.
  const stale = q.isError || (q.dataUpdatedAt > 0 &&
                              Date.now() - q.dataUpdatedAt > 60_000);
  const down = stale || (!u && !q.isLoading);
  const perMin = u?.per_minute ?? 0;
  const hour = u?.last_60m ?? 0;
  const n = (v: number) => (u && !stale ? String(v) : '—');
  const series = Array.isArray(u?.series_60m) ? u!.series_60m! : null;

  return (
    <div className="llm-meter" ref={box}>
      <button
        className="llm-meter-pill"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        title={u && !stale
          ? `${perMin} LLM request(s) in the last minute · ${u.last_15m} in 15 min · `
            + `${hour} in the last hour · ${u.total} since the API started. `
            + 'Counted at the wire, retries included. Click for the breakdown.'
          : down ? 'LLM meter unreachable — this is not "no requests"'
                 : 'LLM requests sent by AIForge'}
      >
        <span className="llm-meter-bolt" style={{ color: rateTone(perMin) }}>⚡</span>
        <span style={{ color: u && !stale ? rateTone(perMin) : 'var(--fg-2)' }}>{n(perMin)}/min</span>
        <span className="llm-meter-sep">·</span>
        <span>{n(hour)} 1h</span>
        {!!u && !stale && u.queued > 0 && (
          // Being throttled is not the same as being slow. Say so, or a capped
          // box reads as a broken one.
          <span style={{ color: 'var(--warn)' }}>· ⏳ {u.queued}</span>
        )}
      </button>

      {open && (
        <div className="llm-meter-panel" role="dialog" aria-label="LLM requests">
          <div className="llm-meter-head">
            <b>LLM requests</b>
            <span className="llm-meter-sub">every call at the wire — chat, pipeline, jobs, memory</span>
          </div>

          <div className="llm-meter-stats">
            <div><span>{n(perMin)}</span><small>last min</small></div>
            <div><span>{n(u?.last_15m ?? 0)}</span><small>last 15 min</small></div>
            <div><span>{n(hour)}</span><small>last hour</small></div>
            <div><span>{n(u?.total ?? 0)}</span><small>since start</small></div>
          </div>

          {series && <>
            <Sparkline data={series} />
            <div className="llm-meter-label">per minute, last 60 min</div>
          </>}

          {!!u?.limit_rpm && (
            <div className="llm-meter-note" style={{ color: 'var(--fg-2)' }}>
              capped at {u.limit_rpm}/min{u.queued > 0
                ? ` · ${u.queued} call(s) waiting` : ''} — Settings → Agent limits
            </div>
          )}
          <Rows title="by role (last hour)" data={u?.by_role || {}} />
          <Rows title="by model (last hour)" data={u?.by_model || {}} />

          {u?.rate_capped && (
            <div className="llm-meter-note">
              rate buffer overflowed — the per-minute figure is a floor
            </div>
          )}
          {down && <div className="llm-meter-note">meter unreachable</div>}
        </div>
      )}
    </div>
  );
}


/** The meter lives in the toolbar, OUTSIDE the route ErrorBoundary — a throw
 *  here would blank the whole app on every page. It gets its own.
 *
 *  The fallback is VISIBLE on purpose. Rendering null made a crashed meter
 *  indistinguishable from a meter that was never deployed, which is exactly
 *  the question someone asks when they cannot find it ("I pulled, I rebuilt,
 *  it still is not there"). A broken meter should say it is broken. */
export default class LlmMeter extends React.Component<{}, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(err: unknown) { console.error('LlmMeter crashed', err); }
  render() {
    if (!this.state.failed) return <LlmMeterInner />;
    return (
      <span className="llm-meter-pill" style={{ cursor: 'default' }}
            title="The LLM request meter failed to render — see the browser console.">
        ⚡ n/a
      </span>
    );
  }
}
