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
import { fmtTokens as fmtK } from '../views/Chat.helpers';

/** Rate colour: neutral up to 20/min, amber to 60, red above — 60/min being
 *  the threshold the chat footer already flags. */
function rateTone(perMin: number): string {
  if (perMin > 60) return 'var(--err)';        // the chat footer's own threshold
  if (perMin > 20) return 'var(--warn)';
  return 'var(--fg-2)';
}

/** One bar per minute. A minute's FAILED share is drawn in the error colour at
 *  the base of its own bar, so a wall of red is legible at a glance without a
 *  second chart — and a bar that is entirely red (every attempt in that minute
 *  failed) cannot be mistaken for a busy one. */
function Sparkline({ data, fails }: Readonly<{ data: number[]; fails?: number[] }>) {
  // Scale on whatever HAPPENED in a minute, sends or failures. The backend
  // creates a bucket for a minute that is nothing but failures — a minute whose
  // only event was a call giving up is the most important one the meter can
  // show — and scaling on sends alone drew it at the idle-grey height:
  // identical to nothing happening at all.
  const at = (i: number) => Math.max(0, fails?.[i] ?? 0);
  const max = Math.max(1, ...data.map((v, i) => Math.max(v, at(i))));
  return (
    <div className="llm-meter-spark" aria-hidden="true">
      {data.map((v, i) => {
        const f = at(i);
        const tot = Math.max(v, f);
        const h = Math.max(tot > 0 ? 2 : 1, Math.round((tot / max) * 28));
        const fh = f > 0 ? Math.max(1, Math.round((Math.min(f, tot) / Math.max(1, tot)) * h)) : 0;
        return (
          // key=index is correct here: fixed-slot positional bars over a numeric
          // bucket series (values duplicate, e.g. zeros); slot i IS the identity
          // and the series never reorders. (S6479 exception)
          <span
            key={i}
            style={{
              height: `${h}px`,
              background: tot > 0 ? 'var(--accent)' : 'var(--border-0)',
            }}
          >
            {fh > 0 && <i style={{ height: `${fh}px`, background: 'var(--err)' }} />}
          </span>
        );
      })}
    </div>
  );
}

function Rows({ title, data, tone, fmt }: Readonly<{
  title: string; data: Record<string, number>; tone?: string;
  fmt?: (n: number) => string;
}>) {
  const rows = Object.entries(data || {}).sort((a, b) => b[1] - a[1]).slice(0, 8);
  if (!rows.length) return null;
  return (
    <div className="llm-meter-rows">
      <div className="llm-meter-label">{title}</div>
      {rows.map(([k, v]) => (
        <div key={k} className="llm-meter-row" style={tone ? { color: tone } : undefined}>
          <span>{k}</span><b>{fmt ? fmt(v) : v}</b>
        </div>
      ))}
    </div>
  );
}

/** One window's number, with the failed share underneath it. The failed count
 *  is a SUBSET of the number above, so it is shown as "of which", never
 *  subtracted — the requests were sent either way. */
function Stat({ n, failed, label, show }: Readonly<{
  n: string; failed: number; label: string; show: boolean;
}>) {
  return (
    <div>
      <span>{n}</span>
      <small>
        {label}
        {show && failed > 0 && (
          <b style={{ color: 'var(--err)', fontWeight: 600 }}> · {failed} failed</b>
        )}
      </small>
    </div>
  );
}

// Close the panel on an outside click or Escape, but only while it is open.
function useClickAway(
  open: boolean,
  box: React.RefObject<HTMLDivElement | null>,
  setOpen: React.Dispatch<React.SetStateAction<boolean>>,
) {
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
}

// The pill/tooltip text, split by meter state (live · unreachable · idle).
function meterTitleText(
  u: LlmUsage | undefined, stale: boolean, down: boolean,
  perMin: number, hour: number, failMin: number, failHour: number,
): string {
  if (u && !stale) {
    return `${perMin} LLM request(s) in the last minute · ${u.last_15m} in 15 min · `
      + `${hour} in the last hour · ${u.total} since the API started. `
      + (failHour
          ? `${failMin} of the last minute and ${failHour} of the last hour `
            + 'came back with no answer (still counted — they went out). '
          : '')
      + 'Counted at the wire, retries included. Click for the breakdown.';
  }
  if (down) {
    return 'LLM meter unreachable — this is not "no requests"';
  }
  return 'LLM requests sent by AIForge';
}

// The toolbar pill — live rate, hour count, failure/queue badges.
function MeterPill({ open, setOpen, meterTitle, perMin, hour, u, stale, failMin, n }: Readonly<{
  open: boolean; setOpen: React.Dispatch<React.SetStateAction<boolean>>; meterTitle: string;
  perMin: number; hour: number; u: LlmUsage | undefined; stale: boolean; failMin: number;
  n: (v: number) => string;
}>) {
  const live = !!u && !stale;
  return (
    <button type="button"
      className="llm-meter-pill"
      onClick={() => setOpen(o => !o)}
      aria-expanded={open}
      title={meterTitle}
    >
      <span className="llm-meter-bolt" style={{ color: rateTone(perMin) }}>⚡</span>
      <span style={{ color: live ? rateTone(perMin) : 'var(--fg-2)' }}>{n(perMin)}/min</span>
      <span className="llm-meter-sep">·</span>
      <span>{n(hour)} 1h</span>
      {live && failMin > 0 && (
        // Failures are shown NEXT TO the rate, never subtracted from it: the
        // requests were sent. "40/min · ✕38" is the reading that says retry
        // storm; a rate with the failures quietly removed would read 2.
        <span style={{ color: 'var(--err)' }} title={`${failMin} failed in the last minute`}>
          · ✕{failMin}
        </span>
      )}
      {live && u!.queued > 0 && (
        // Being throttled is not the same as being slow. Say so, or a capped
        // box reads as a broken one.
        <span style={{ color: 'var(--warn)' }}
              title={(u!.held_s ?? 0) > 0
                ? `the provider rejected us — holding ${Math.round(u!.held_s!)}s`
                : `${u!.queued} call(s) waiting on your ${u!.limit_rpm}/min ceiling`}>
          · ⏳ {u!.queued}
        </span>
      )}
    </button>
  );
}

// The expandable panel's footnotes — ceilings, throttle holds, token totals.
function MeterNotes({ u, down }: Readonly<{ u: LlmUsage | undefined; down: boolean }>) {
  return (
    <>
      {!!u?.limit_rpm && (
        <div className="llm-meter-note" style={{ color: 'var(--fg-2)' }}>
          capped at {u.limit_rpm}/min{typeof u.limit_used === 'number'
            ? ` · ${u.limit_used} used` : ''}{u.queued > 0
            ? ` · ${u.queued} call(s) waiting` : ''} — Settings → Agent limits
        </div>
      )}
      {u?.limit_scope === 'process' && (
        // The ceiling is meant to be machine-wide. When the shared store is
        // unavailable each process counts alone, so the real rate is this
        // number TIMES the number of AIForge processes — which looks
        // exactly like "the setting is not working".
        <div className="llm-meter-note" style={{ color: 'var(--warn)' }}>
          ⚠ counting THIS PROCESS ONLY — the shared window is unavailable,
          so the machine-wide rate is a multiple of any ceiling you set, and
          a provider rejection seen by one process will not hold the others.
          See the aiforge.rate_limiter log for why.
        </div>
      )}
      {(u?.held_s ?? 0) > 0 && (
        // The reason the ⏳ badge can appear with NO ceiling set: the
        // provider refused us and we are obeying it. Without this line an
        // operator running limit_rpm=0 sees a queue with no explanation.
        <div className="llm-meter-note" style={{ color: 'var(--warn)' }}>
          the provider rejected us for sending too fast — holding{' '}
          {Math.round(u!.held_s!)}s before the next call
        </div>
      )}
      {!!(u?.tokens_out_60m ?? 0) && (
        <div className="llm-meter-note" style={{ color: 'var(--fg-2)' }}>
          {fmtK(u!.tokens_out_60m)} tokens written · {fmtK(u!.tokens_in_60m)} sent
          &nbsp;(last hour, as the provider counted them)
        </div>
      )}
      {/* Same quantity as the line above, so the same units: raw
          integers here read as a third, unrelated "chat" row. */}
      <Rows title="tokens written (last hour)"
            data={u?.tokens_out_by_role || {}} fmt={fmtK} />
      <Rows title="by role (last hour)" data={u?.by_role || {}} />
      <Rows title="by model (last hour)" data={u?.by_model || {}} />
      <Rows title="failed (last hour)" data={u?.by_fail_reason || {}}
            tone="var(--err)" />

      {u?.rate_capped && (
        <div className="llm-meter-note">
          rate buffer overflowed — the per-minute figure is a floor
        </div>
      )}
      {down && <div className="llm-meter-note">meter unreachable</div>}
    </>
  );
}

// The expandable panel — per-window stats, sparkline, and footnotes.
function MeterPanel({ u, stale, n, perMin, hour, failMin, failHour, series, failSeries, down }: Readonly<{
  u: LlmUsage | undefined; stale: boolean; n: (v: number) => string;
  perMin: number; hour: number; failMin: number; failHour: number;
  series: number[] | null; failSeries: number[] | undefined; down: boolean;
}>) {
  const live = !!u && !stale;
  return (
    <div className="llm-meter-panel" role="dialog" aria-label="LLM requests">
      <div className="llm-meter-head">
        <b>LLM requests</b>
        <span className="llm-meter-sub">every call at the wire — chat, pipeline, jobs, memory</span>
      </div>

      <div className="llm-meter-stats">
        <Stat n={n(perMin)} failed={failMin} label="last min" show={live} />
        <Stat n={n(u?.last_15m ?? 0)} failed={u?.failed_15m ?? 0}
              label="last 15 min" show={live} />
        <Stat n={n(hour)} failed={failHour} label="last hour" show={live} />
        <Stat n={n(u?.total ?? 0)} failed={u?.failed ?? 0}
              label="since start" show={live} />
      </div>

      {series && <>
        <Sparkline data={series} fails={failSeries} />
        <div className="llm-meter-label">
          per minute, last 60 min{failHour ? ' — red = no answer' : ''}
        </div>
      </>}

      <MeterNotes u={u} down={down} />
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

  useClickAway(open, box, setOpen);

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
  // `?? 0` throughout: a browser can be polling an API that predates the
  // failure fields, and an undefined must read as "no failures reported", not
  // render "NaN failed".
  const failMin = u?.failed_per_minute ?? 0;
  const failHour = u?.failed_60m ?? 0;
  const n = (v: number) => (u && !stale ? String(v) : '—');
  const series = Array.isArray(u?.series_60m) ? u!.series_60m! : null;
  const failSeries = Array.isArray(u?.series_fail_60m) ? u!.series_fail_60m! : undefined;

  const meterTitle = meterTitleText(u, stale, down, perMin, hour, failMin, failHour);

  return (
    <div className="llm-meter" ref={box}>
      <MeterPill open={open} setOpen={setOpen} meterTitle={meterTitle} perMin={perMin}
                 hour={hour} u={u} stale={stale} failMin={failMin} n={n} />
      {open && (
        <MeterPanel u={u} stale={stale} n={n} perMin={perMin} hour={hour} failMin={failMin}
                    failHour={failHour} series={series} failSeries={failSeries} down={down} />
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
