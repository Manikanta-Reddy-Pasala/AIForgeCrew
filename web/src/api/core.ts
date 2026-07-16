// Minimal fetch wrapper against the FastAPI backend.
export const BASE = '/api';

export async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, init);
  if (!r.ok) {
    // Read the body ONCE as text, then try to parse JSON — reading .json()
    // first and .text() in the catch double-reads the stream ("body stream
    // already read") and discards a non-JSON (proxy HTML) error body.
    let detail = '';
    try {
      const raw = await r.text();
      try { const b = JSON.parse(raw); detail = b?.detail || b?.error || raw; }
      catch { detail = raw; }
    } catch { /* ignore */ }
    const suffix = detail ? ` — ${detail}` : '';
    throw new Error(`${r.status} ${r.statusText}${suffix}`);
  }
  return r.json();
}

export function logStreamURL(role: string): string {
  return `${BASE}/logs/${role}/stream`;
}
