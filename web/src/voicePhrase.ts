/** One dictated phrase, and where it was inserted in the composer. */
export type SpokenAnchor = {
  start: number;
  raw: string;
  epoch: number;
};

/** Where `spoken` will sit once it is appended to `prev`. */
export function planSpokenInsert(prev: string, spoken: string): { text: string; start: number } {
  const next = spoken.trim().replace(/\s+/g, ' ');
  if (!next) return { text: prev, start: -1 };
  if (!prev.trim()) return { text: next, start: 0 };
  const base = prev.trimEnd();
  return { text: `${base} ${next}`, start: base.length + 1 };
}

/**
 * Replace the phrase at `anchor.start` when that slice is still the whisper
 * text. Later anchors, held by in-flight tidies, move by the length change.
 * Returns the composer text unchanged when the slice was edited or the draft
 * moved on.
 */
export function applySpokenCorrection(
  prev: string,
  anchors: SpokenAnchor[],
  anchor: SpokenAnchor,
  corrected: string,
  epoch: number,
): string {
  const to = corrected.trim().replace(/\s+/g, ' ');
  if (anchor.epoch !== epoch || anchor.start < 0 || !to || to === anchor.raw) return prev;
  const at = anchor.start;
  if (prev.slice(at, at + anchor.raw.length) !== anchor.raw) return prev;
  const before = prev.slice(0, at);
  const after = prev.slice(at + anchor.raw.length);
  const leftOk = before === '' || /\s$/.test(before);
  const rightOk = after === '' || /^\s/.test(after);
  if (!leftOk || !rightOk) return prev;
  const delta = to.length - anchor.raw.length;
  for (const other of anchors) {
    if (other !== anchor && other.start > at) other.start += delta;
  }
  return before + to + after;
}
