// Server-Sent Events, as the AIForge API sends them: one JSON object per
// `data:` line. Chunks arrive split anywhere, so the parser keeps the partial
// line it has not seen the end of. A line that is not JSON is framing or a
// comment and must not end a two-hour run (same rule as the CLI's
// parse_sse_line).

export type AgentEvent = { type?: string; [key: string]: unknown };

export function parseSseLine(line: string): AgentEvent | null {
  if (!line.startsWith('data:')) return null;
  const payload = line.slice(5).trim();
  if (!payload) return null;
  try {
    const ev = JSON.parse(payload);
    return ev && typeof ev === 'object' && !Array.isArray(ev) ? (ev as AgentEvent) : null;
  } catch {
    return null;
  }
}

export class SseParser {
  private buf = '';

  /** Events completed by this chunk, in order. */
  feed(chunk: string): AgentEvent[] {
    this.buf += chunk.replace(/\r\n?/g, '\n');
    const lines = this.buf.split('\n');
    this.buf = lines.pop() ?? '';
    const out: AgentEvent[] = [];
    for (const line of lines) {
      const ev = parseSseLine(line);
      if (ev) out.push(ev);
    }
    return out;
  }

  /** Whatever the stream ended on without a newline. */
  flush(): AgentEvent[] {
    const rest = this.buf;
    this.buf = '';
    const ev = parseSseLine(rest);
    return ev ? [ev] : [];
  }
}
