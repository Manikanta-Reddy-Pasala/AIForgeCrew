// Minimal fetch wrapper against the FastAPI backend.
export const BASE = '/api';

// Shared API token. The backend trusts loopback without a token, so a browser
// on the same machine needs nothing here — this is only for a UI opened from
// ANOTHER host, where the request is remote and must authenticate. One shared
// secret, no login/session: set it once from the console with
//   localStorage.setItem('aiforge_api_token', '<token>')
export const TOKEN_KEY = 'aiforge_api_token';

export function apiToken(): string {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

/** Auth headers for an API call — empty when no token is configured. */
export function authHeaders(): Record<string, string> {
  const t = apiToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/**
 * fetch() against the API with the token attached. Every API call goes
 * through here (or through `j`) so there is ONE place that knows about auth.
 */
export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = { ...authHeaders(), ...(init?.headers as Record<string, string> | undefined) };
  return fetch(`${BASE}${path}`, { ...init, headers });
}

export async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await apiFetch(path, init);
  if (r.status === 401) {
    throw new Error(
      `401 — this AIForge requires an API token for non-local access. Run ` +
      `localStorage.setItem('${TOKEN_KEY}', '<token>') in the browser console ` +
      `(the AIFORGE_API_TOKEN value from the server) and reload. Browsing from ` +
      `the server machine itself needs no token.`,
    );
  }
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
