/* mdlite — a compact, zero-dependency markdown renderer for chat answers
 * and action previews.
 *
 * Block level:  # headings, fenced ```code``` (with language label), GFM
 *   tables, > blockquotes, --- horizontal rules, ordered (1.) and unordered
 *   (-, *, +) lists nested by indentation, blank-line paragraphs.
 * Inline level: **bold**, *italic* / _italic_, `code`, [text](url), and bare
 *   http(s) URLs (auto-linked). Formatting nests (bold inside a list item,
 *   code inside bold, …) except inside `code` and links, which stay literal.
 *
 * Kept dependency-free on purpose (deploy-anywhere clone-and-run) — no
 * react-markdown / remark transitive tree.
 */
import React from 'react';
import { legacyCopy } from './util';

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
  return legacyCopy(text) ? Promise.resolve() : Promise.reject(new Error('copy failed'));
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
// Earliest-match tokenizer over a list of SIMPLE patterns rather than one
// alternation (which scored cognitive complexity 46). List ORDER is the
// tie-break for two matches at the same index: ** before *, __ before _, so
// bold wins over italic.
//
// Measured, not assumed: the old bold branches could not actually backtrack —
// `[^*]` and `\*(?!\*)` never match the same character — so splitting them was
// for readability. The one GENUINELY quadratic case was the link: `\[[^\]]+`
// lets link text contain `[`, so on a run of unclosed brackets every start
// position scanned to the end of the string (20 KB of `[` took ~160 ms, in the
// old pattern and in a naive split alike). Link text now excludes `[`, which
// stops each attempt at the very next bracket.
const INLINE_PATTERNS: readonly RegExp[] = [
  /`[^`]+`/,                                  // code
  /\*\*[^*]+(?:\*[^*]+)*\*\*/,                   // **bold** (may hold a lone *)
  /__[^_]+(?:_[^_]+)*__/,                     // __bold__
  /\*[^*\n]+\*/,                               // *italic*
  /_[^_\n]+_/,                                 // _italic_
  /\[[^[\]]+\]\([^)\s]+\)/,                     // [text](url) — text holds no [
  /\bhttps?:\/\/[^\s<>()]+/,                     // bare URL
];

/** The earliest inline token in `text`, or null. */
function nextToken(text: string): { index: number; tok: string } | null {
  let best: { index: number; tok: string } | null = null;
  for (const re of INLINE_PATTERNS) {
    const m = re.exec(text);
    if (m && (best === null || m.index < best.index)) {
      best = { index: m.index, tok: m[0] };
    }
  }
  return best;
}

function renderInline(text: string, key: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let rest = text;
  let n = 0;
  while (rest) {
    const m = nextToken(rest);
    if (!m) { out.push(rest); break; }
    if (m.index > 0) out.push(rest.slice(0, m.index));
    const { tok } = m;
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
        return <div key={j} style={{ color, background, padding: '0 4px' }}>{ln || ' '}</div>; // NOSONAR
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
      <thead><tr>{header.map((c, ci) => <th key={ci}>{renderInline(c, `th-${k + 1}-${ci}`)}</th>) /* NOSONAR */}</tr></thead>
      <tbody>
        {rows.map((r, ri) => (
          <tr key={ri} /* NOSONAR */>{header.map((_, ci) => <td key={ci}>{renderInline(r[ci] ?? '', `td-${k + 1}-${ri}-${ci}`)}</td>) /* NOSONAR */}</tr>
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

// Lists — ordered (1. / 1)) and unordered (- * +), NESTED by indentation.
// The model writes "2. **Grammar fixes:**" then "   - item" under it; flat
// parsing ended the numbered list there, drew the sub-points flush with the
// numbers, and restarted the numbering at the next item. Rules:
//  * an item indented deeper than the current one nests under it;
//  * bullets right after a numbered item belong to it even when the model did
//    not indent them (the common "1. Heading:" / "- point" shape);
//  * blank lines between items do not end the list (LLM output routinely
//    blank-separates items — breaking there restarted every item at 1);
//  * an indented non-item line continues the item above it.
const ITEM_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

type ListItem = { text: string[]; children: ListNode[] };
type ListNode = { ordered: boolean; start: number; indent: number; items: ListItem[] };

function indentOf(ws: string): number {
  return ws.replace(/\t/g, '    ').length;
}

function parseItem(line: string): { indent: number; ordered: boolean; num: number; text: string } | null {
  const m = ITEM_RE.exec(line);
  if (!m) return null;
  const ordered = /\d/.test(m[2]);
  return { indent: indentOf(m[1]), ordered, num: ordered ? Number.parseInt(m[2], 10) || 1 : 1, text: m[3] };
}

// The next non-blank line from j, or -1.
function nextFilled(lines: string[], j: number): number {
  while (j < lines.length && !lines[j].trim()) j++;
  return j < lines.length ? j : -1;
}

function parseList(lines: string[], i: number, baseIndent: number, ordered: boolean): { node: ListNode; next: number } {
  const first = parseItem(lines[i])!;
  const node: ListNode = { ordered, start: first.num, indent: baseIndent, items: [] };
  let j = i;
  while (j < lines.length) {
    const line = lines[j];
    if (!line.trim()) {
      const n = nextFilled(lines, j);
      const it = n >= 0 ? parseItem(lines[n]) : null;
      // A blank line ends the list unless the list goes on after it.
      const continues = it && (it.indent > baseIndent
        || (it.indent === baseIndent && it.ordered === ordered));
      if (!continues) break;
      j = n;
      continue;
    }
    const it = parseItem(line);
    const last = node.items[node.items.length - 1];
    if (it && it.indent === baseIndent && it.ordered === ordered) {
      node.items.push({ text: [it.text], children: [] });
      j++;
    } else if (it && last && (it.indent > baseIndent || (ordered && !it.ordered && it.indent === baseIndent))) {
      const sub = parseList(lines, j, it.indent, it.ordered);
      last.children.push(sub.node);
      j = sub.next;
    } else if (!it && last && indentOf(/^\s*/.exec(line)![0]) > baseIndent) {
      last.text.push(line.trim());              // continuation of the item above
      j++;
    } else {
      break;
    }
  }
  return { node, next: j };
}

function renderList(node: ListNode, key: string): React.ReactNode {
  /* key=index: immutable parsed items, may duplicate, never reorder. (S6479 exception) */
  const items = node.items.map((it, idx) => (
    <li key={idx} /* NOSONAR */>
      {it.text.map((t, ti) => (
        <React.Fragment key={ti} /* NOSONAR */>
          {ti > 0 && <br />}
          {renderInline(t, `${key}-${idx}-${ti}`)}
        </React.Fragment>
      ))}
      {it.children.map((c, ci) => renderList(c, `${key}-${idx}-c${ci}`))}
    </li>
  ));
  return node.ordered
    ? <ol key={key} start={node.start}>{items}</ol>
    : <ul key={key}>{items}</ul>;
}

function listBlock(lines: string[], i: number, k: number): Block {
  const it = parseItem(lines[i]);
  if (!it) return null;
  const { node, next } = parseList(lines, i, it.indent, it.ordered);
  return { node: renderList(node, `list-${k}`), next, k: k + 1 };
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
    !ITEM_RE.test(lines[j])
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
        <React.Fragment key={idx} /* NOSONAR */>
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
      ?? listBlock(lines, i, k)
      ?? paragraphBlock(lines, i, k);
    out.push(r.node);
    i = r.next;
    k = r.k;
  }
  return <>{out}</>;
}
