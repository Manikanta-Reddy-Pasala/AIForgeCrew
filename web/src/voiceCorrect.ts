// After Whisper writes a line, the chat model tidies it. The raw line is
// already in the box; the caller swaps it only when this comes back different.

import { j } from './api/core';

const CORRECT_MS = 10_000;

export async function correctSpoken(raw: string): Promise<string> {
  const text = raw.trim();
  if (text.length < 2) return text;
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), CORRECT_MS);
  try {
    const res = await j<{ text?: string }>('/chat/speech-correct', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
      signal: ctrl.signal,
    });
    const fixed = (res.text || '').replace(/\s+/g, ' ').trim();
    return fixed || text;
  } catch {
    return text;
  } finally {
    window.clearTimeout(timer);
  }
}
