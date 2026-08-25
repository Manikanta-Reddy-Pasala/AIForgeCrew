/* mdlite — a compact, zero-dependency markdown renderer for chat answers
 * and action previews.
 *
 * Block level:  # headings, fenced ```code``` (with language label), GFM
 *   tables, > blockquotes, --- horizontal rules, ordered (1.) and unordered
 *   (-, *) lists, blank-line paragraphs.
 * Inline level: **bold**, *italic* / _italic_, `code`, [text](url), and bare
 *   http(s) URLs (auto-linked). Formatting nests (bold inside a list item,
 *   code inside bold, …) except inside `code` and links, which stay literal.
 *
 * Kept dependency-free on purpose (deploy-anywhere clone-and-run) — no
 * react-markdown / remark transitive tree.
 */
import React from 'react';

// Allow only safe link schemes — reject javascript:/data:/vbscript: etc. so a
// model-emitted [x](javascript:…) link can't run script in the app origin.
// Returns the href if safe, or '' to drop it.
function safeHref(url: string): string {
  const u = (url || '').trim();
  if (u.startsWith('//')) return '';                       // protocol-relative → open-redirect, drop
  if (/^(https?:|mailto:|tel:)/i.test(u)) return u;       // explicit safe schemes
  if (/^[/#?]/.test(u)) return u;                          // relative path / anchor / query
  if (!/^[a-z][a-z0-9+.-]*:/i.test(u)) return u;           // no scheme → relative
  return '';                                               // any other scheme → drop
}

// ── copy-to-clipboard ────────────────────────────────────────────────────────
// navigator.clipboard needs a SECURE context (https / localhost); the app is
// often reached over plain http on a LAN IP where it's undefined — fall back to
// a hidden-textarea execCommand so Copy works everywhere.
export function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.top = '-9999px';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      ok ? resolve() : reject(new Error('copy failed'));
    } catch (e) { reject(e as Error); }
  });
}

/** A small Copy button that flips to a check for ~1.2s. Reused for code blocks,
 *  full answers, and user messages. */
export function CopyButton(
  { text, label = 'Copy', title = 'Copy', className, style }:
  Readonly<{ text: string; label?: string; title?: string;
    className?: string; style?: React.CSSProperties }>,
) {
  const [done, setDone] = React.useState(false);
  const onCopy = React.useCallback(() => {
    copyText(text).then(() => {
      setDone(true);
      setTimeout(() => setDone(false), 1200);
    }).catch(() => {});
  }, [text]);
  return (
    <button type="button" onClick={onCopy} title={title}
            className={className} style={style}>
      {done ? '✓ Copied' : label}
    </button>
  );
}

// Wraps a fenced-code <pre> and floats a Copy button in its top-right corner.
function CodeFence({ body, children }:
    Readonly<{ body: string; children: React.ReactNode }>) {
  return (
    <div className="mdlite-fence" style={{ position: 'relative' }}>
      <CopyButton text={body} title="Copy code" className="mdlite-copy"
        style={{
          position: 'absolute', top: 6, right: 6, zIndex: 1,
          font: '11px var(--font-sans, sans-serif)', cursor: 'pointer',
          padding: '2px 7px', borderRadius: 5, opacity: 0.75,
          border: '1px solid var(--border, #3a3a3a)',
          background: 'var(--bg-2, #2a2a2a)', color: 'var(--fg-2, #ccc)',
        }} />
      {children}
    </div>
  );
}

// ── inline ──────────────────────────────────────────────────────────────────
// Earliest-match tokenizer. Order in the alternation matters: ** before *,
// __ before _, so bold wins over italic.
const INLINE_RE =
  /(`[^`]+`)|(\*\*(?:[^*]|\*(?!\*))+\*\*)|(__(?:[^_]|_(?!_))+__)|(\*[^*\n]+?\*)|(_[^_\n]+?_)|(\[[^\]]+\]\([^)\s]+\))|(\bhttps?:\/\/[^\s<>()]+)/;

function renderInline(text: string, key: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let rest = text;
  let n = 0;
  while (rest) {
    const m = INLINE_RE.exec(rest);
    if (!m) { out.push(rest); break; }
    if (m.index > 0) out.push(rest.slice(0, m.index));
    const tok = m[0];
    const kk = `${key}-${n++}`;
    if (tok.startsWith('`')) {
      out.push(<code key={kk}>{tok.slice(1, -1)}</code>);
    } else if (tok.startsWith('**') || tok.startsWith('__')) {
      out.push(<strong key={kk}>{renderInline(tok.slice(2, -2), kk)}</strong>);
    } else if (tok.startsWith('[')) {
      const mm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(tok)!;
      // Only allow safe schemes — a model-emitted [x](javascript:…) link would
      // otherwise execute script in the app origin on click (XSS).
      const href = safeHref(mm[2]);
      out.push(href
        ? <a key={kk} href={href} target="_blank" rel="noopener noreferrer">{mm[1]}</a>
        : <span key={kk}>{mm[1]}</span>);
    } else if (/^https?:\/\//.test(tok)) {
      out.push(<a key={kk} href={tok} target="_blank" rel="noopener noreferrer">{tok}</a>);
    } else { // * or _ italic
      out.push(<em key={kk}>{renderInline(tok.slice(1, -1), kk)}</em>);
    }
    rest = rest.slice(m.index + tok.length);
  }
  return out;
}

function splitRow(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
}

// ── block ───────────────────────────────────────────────────────────────────
// Each block handler consumes one or more lines starting at `i` and returns the
// rendered node plus the next line index and next key counter — or null when
// this handler does not apply. MdLite is then a thin dispatcher over them.
type Block = { node: React.ReactNode; next: number; k: number } | null;

// Color for one line of a unified-diff fence (+/-/@@ headers).
function diffLineStyle(ln: string): { color?: string; background?: string } {
  if (ln.startsWith('+++') || ln.startsWith('---') || /^(diff |index )/.test(ln)) {
    return { color: 'var(--fg-3)' };
  }
  if (ln.startsWith('+')) {
    return { color: 'var(--ok, #3fb950)', background: 'rgba(63,185,80,0.10)' };
  }
  if (ln.startsWith('-')) {
    return { color: 'var(--err, #e5534b)', background: 'rgba(229,83,75,0.10)' };
  }
  if (ln.startsWith('@@')) {
    return { color: '#6aa6ff' };
  }
  return {};
}

// Color a unified-diff fence (+/-/@@) line-by-line instead of a flat <code>
// block, so an approval preview's code changes read like a diff.
function renderDiffFence(body: string, k: number): React.ReactNode {
  return (
    <CodeFence key={`p-${k}`} body={body}>
    <pre data-lang="diff" style={{
      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      fontFamily: 'var(--font-mono)', fontSize: 12, lineHeight: 1.45,
    }}>
      {body.split('\n').map((ln, j) => {
        const { color, background } = diffLineStyle(ln);
        // key=index: immutable fence text rendered once; diff lines
        // legitimately duplicate and never reorder. (S6479 exception)
        return <div key={j} style={{ color, background, padding: '0 4px' }}>{ln || ' '}</div>;
      })}
    </pre>
    </CodeFence>
  );
}

// fenced code (```lang ... ```)
function fenceBlock(lines: string[], i: number, k: number): Block {
  const line = lines[i];
  if (!/^\s*```/.test(line)) return null;
  const lang = line.replace(/^\s*```/, '').trim();
  const end = lines.findIndex((l, j) => j > i && /^\s*```/.test(l));
  const stop = end === -1 ? lines.length : end;
  const body = lines.slice(i + 1, stop).join('\n');
  const node = lang === 'diff' ? renderDiffFence(body, k) : (
    <CodeFence key={`p-${k}`} body={body}>
    <pre data-lang={lang || undefined}>
      <code>{body}</code>
    </pre>
    </CodeFence>
  );
  return { node, next: stop + 1, k: k + 1 };
}

// heading (# … ######)
function headingBlock(lines: string[], i: number, k: number): Block {
  const h = /^(#{1,6})\s+(\S.*|)$/.exec(lines[i]);
  if (!h) return null;
  const lvl = h[1].length;
  const Tag = (`h${lvl}` as keyof JSX.IntrinsicElements);
  return { node: <Tag key={`h-${k}`}>{renderInline(h[2], `h-${k + 1}`)}</Tag>,
           next: i + 1, k: k + 1 };
}

// horizontal rule
function hrBlock(lines: string[], i: number, k: number): Block {
  if (!/^\s*([-*_])\1{2,}\s*$/.test(lines[i])) return null;
  return { node: <hr key={`hr-${k}`} />, next: i + 1, k: k + 1 };
}

// GFM table: header row + |---|---| separator
function tableBlock(lines: string[], i: number, k: number): Block {
  const line = lines[i];
  if (!(line.includes('|') && i + 1 < lines.length &&
        /^[\s:|-]*$/.test(lines[i + 1]) && lines[i + 1].includes('-'))) return null;
  const header = splitRow(line);
  let j = i + 2;
  const rows: string[][] = [];
  while (j < lines.length && lines[j].includes('|') && lines[j].trim()) {
    rows.push(splitRow(lines[j]));
    j++;
  }
  const node = (
    // key=index throughout this table: a pure render of immutable parsed
    // text — header/cell text and whole rows legitimately duplicate (a
    // content key would collide) and column/row order is positional and
    // never reorders. (S6479 exception)
    <table key={`tb-${k}`} className="md-table">
      <thead><tr>{header.map((c, ci) => <th key={ci}>{renderInline(c, `th-${k + 1}-${ci}`)}</th>)}</tr></thead>
      <tbody>
        {rows.map((r, ri) => (
          <tr key={ri}>{header.map((_, ci) => <td key={ci}>{renderInline(r[ci] ?? '', `td-${k + 1}-${ri}-${ci}`)}</td>)}</tr>
        ))}
      </tbody>
    </table>
  );
  return { node, next: j, k: k + 1 };
}

// blockquote (collapse consecutive > lines)
function blockquoteBlock(lines: string[], i: number, k: number): Block {
  if (!/^\s*>\s?/.test(lines[i])) return null;
  const buf: string[] = [];
  let j = i;
  while (j < lines.length && /^\s*>\s?/.test(lines[j])) {
    buf.push(lines[j].replace(/^\s*>\s?/, ''));
    j++;
  }
  return { node: <blockquote key={`bq-${k}`}>{renderInline(buf.join(' '), `bq-${k + 1}`)}</blockquote>,
           next: j, k: k + 1 };
}

// ordered list (1. 2. …). Keep numbered items in ONE list across blank lines —
// LLM output routinely blank-separates items, and breaking there made each item
// its own <ol> that restarts at 1 (every item showed "1"). Honor the source's
// first number via `start` so a list that begins at N renders from N.
function orderedListBlock(lines: string[], i: number, k: number): Block {
  const line = lines[i];
  if (!/^\s*\d+\.\s+/.test(line)) return null;
  const startNum = parseInt(line.match(/^\s*(\d+)\./)?.[1] ?? '1', 10) || 1;
  const items: string[] = [];
  let j = i;
  while (j < lines.length) {
    if (/^\s*\d+\.\s+/.test(lines[j])) {
      items.push(lines[j].replace(/^\s*\d+\.\s+/, ''));
      j++;
    } else if (lines[j].trim() === '' && /^\s*\d+\.\s+/.test(lines[j + 1] ?? '')) {
      j++;                       // skip a blank line BETWEEN numbered items
    } else {
      break;
    }
  }
  const node = (
    <ol key={`ol-${k}`} start={startNum}>
      {/* key=index: immutable parsed items, may duplicate, never reorder. (S6479 exception) */}
      {items.map((it, idx) => <li key={idx}>{renderInline(it, `oli-${k + 1}-${idx}`)}</li>)}
    </ol>
  );
  return { node, next: j, k: k + 1 };
}

// unordered list (- or *)
function unorderedListBlock(lines: string[], i: number, k: number): Block {
  if (!/^\s*[-*]\s+/.test(lines[i])) return null;
  const items: string[] = [];
  let j = i;
  while (j < lines.length && /^\s*[-*]\s+/.test(lines[j])) {
    items.push(lines[j].replace(/^\s*[-*]\s+/, ''));
    j++;
  }
  const node = (
    <ul key={`ul-${k}`}>
      {/* key=index: immutable parsed items, may duplicate, never reorder. (S6479 exception) */}
      {items.map((it, idx) => <li key={idx}>{renderInline(it, `li-${k + 1}-${idx}`)}</li>)}
    </ul>
  );
  return { node, next: j, k: k + 1 };
}

// paragraph: gather until a blank line or a block starter. Always applies (the
// dispatcher's fallback), so it never returns null.
function paragraphBlock(lines: string[], i: number, k: number): NonNullable<Block> {
  const pLines: string[] = [];
  let j = i;
  while (
    j < lines.length &&
    lines[j].trim() &&
    !/^\s*```/.test(lines[j]) &&
    !/^#{1,6}\s/.test(lines[j]) &&
    !/^\s*>\s?/.test(lines[j]) &&
    !/^\s*\d+\.\s+/.test(lines[j]) &&
    !/^\s*[-*]\s+/.test(lines[j])
  ) {
    pLines.push(lines[j]);
    j++;
  }
  // Preserve intentional soft line breaks inside a paragraph (the container is
  // no longer white-space:pre-wrap) while still running each line through the
  // inline tokenizer.
  const node = (
    <p key={`para-${k}`}>
      {/* key=index: soft-wrapped lines of one immutable paragraph; positional,
          may duplicate, never reorder. (S6479 exception) */}
      {pLines.map((pl, idx) => (
        <React.Fragment key={idx}>
          {idx > 0 && <br />}
          {renderInline(pl, `p-${k + 1}-${idx}`)}
        </React.Fragment>
      ))}
    </p>
  );
  return { node, next: j, k: k + 1 };
}

export function MdLite({ text }: Readonly<{ text: string }>) {
  if (!text) return null;
  const out: React.ReactNode[] = [];
  const lines = text.split('\n');
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    if (!lines[i].trim()) { i++; continue; }   // blank line → paragraph break
    const r = fenceBlock(lines, i, k)
      ?? headingBlock(lines, i, k)
      ?? hrBlock(lines, i, k)
      ?? tableBlock(lines, i, k)
      ?? blockquoteBlock(lines, i, k)
      ?? orderedListBlock(lines, i, k)
      ?? unorderedListBlock(lines, i, k)
      ?? paragraphBlock(lines, i, k);
    out.push(r.node);
    i = r.next;
    k = r.k;
  }
  return <>{out}</>;
}
