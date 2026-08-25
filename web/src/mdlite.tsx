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
      document.body.removeChild(ta);
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
  /(`[^`]+`)|(\*\*[\s\S]+?\*\*)|(__[\s\S]+?__)|(\*[^*\n]+?\*)|(_[^_\n]+?_)|(\[[^\]]+\]\([^)\s]+\))|(\bhttps?:\/\/[^\s<>()]+)/;

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
  return line.replace(/^\s*\|?/, '').replace(/\|?\s*$/, '').split('|').map(c => c.trim());
}

// ── block ───────────────────────────────────────────────────────────────────
export function MdLite({ text }: Readonly<{ text: string }>) {
  if (!text) return null;
  const out: React.ReactNode[] = [];
  const lines = text.split('\n');
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i];

    // fenced code (```lang ... ```)
    if (/^\s*```/.test(line)) {
      const lang = line.replace(/^\s*```/, '').trim();
      const end = lines.findIndex((l, j) => j > i && /^\s*```/.test(l));
      const stop = end === -1 ? lines.length : end;
      const body = lines.slice(i + 1, stop).join('\n');
      if (lang === 'diff') {
        // Color a unified-diff fence (+/-/@@) line-by-line instead of a flat
        // <code> block, so an approval preview's code changes read like a diff.
        out.push(
          <CodeFence key={`p-${k++}`} body={body}>
          <pre data-lang="diff" style={{
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            fontFamily: 'var(--font-mono)', fontSize: 12, lineHeight: 1.45,
          }}>
            {body.split('\n').map((ln, j) => {
              let color: string | undefined;
              let background: string | undefined;
              if (ln.startsWith('+++') || ln.startsWith('---') || /^(diff |index )/.test(ln)) {
                color = 'var(--fg-3)';
              } else if (ln.startsWith('+')) {
                color = 'var(--ok, #3fb950)'; background = 'rgba(63,185,80,0.10)';
              } else if (ln.startsWith('-')) {
                color = 'var(--err, #e5534b)'; background = 'rgba(229,83,75,0.10)';
              } else if (ln.startsWith('@@')) {
                color = '#6aa6ff';
              }
              // key=index: immutable fence text rendered once; diff lines
              // legitimately duplicate and never reorder. (S6479 exception)
              return <div key={j} style={{ color, background, padding: '0 4px' }}>{ln || ' '}</div>;
            })}
          </pre>
          </CodeFence>,
        );
        i = stop + 1;
        continue;
      }
      out.push(
        <CodeFence key={`p-${k++}`} body={body}>
        <pre data-lang={lang || undefined}>
          <code>{body}</code>
        </pre>
        </CodeFence>,
      );
      i = stop + 1;
      continue;
    }

    // heading (# … ######)
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const lvl = h[1].length;
      const Tag = (`h${lvl}` as keyof JSX.IntrinsicElements);
      out.push(<Tag key={`h-${k++}`}>{renderInline(h[2], `h-${k}`)}</Tag>);
      i++;
      continue;
    }

    // horizontal rule
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      out.push(<hr key={`hr-${k++}`} />);
      i++;
      continue;
    }

    // GFM table: header row + |---|---| separator
    if (line.includes('|') && i + 1 < lines.length &&
        /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1]) &&
        lines[i + 1].includes('-')) {
      const header = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      out.push(
        // key=index throughout this table: a pure render of immutable parsed
        // text — header/cell text and whole rows legitimately duplicate (a
        // content key would collide) and column/row order is positional and
        // never reorders. (S6479 exception)
        <table key={`tb-${k++}`} className="md-table">
          <thead><tr>{header.map((c, j) => <th key={j}>{renderInline(c, `th-${k}-${j}`)}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>{header.map((_, ci) => <td key={ci}>{renderInline(r[ci] ?? '', `td-${k}-${ri}-${ci}`)}</td>)}</tr>
            ))}
          </tbody>
        </table>,
      );
      continue;
    }

    // blockquote (collapse consecutive > lines)
    if (/^\s*>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      out.push(<blockquote key={`bq-${k++}`}>{renderInline(buf.join(' '), `bq-${k}`)}</blockquote>);
      continue;
    }

    // ordered list (1. 2. …). Keep numbered items in ONE list across blank
    // lines — LLM output routinely blank-separates items, and breaking there
    // made each item its own <ol> that restarts at 1 (every item showed "1").
    // Honor the source's first number via `start` so a list that begins at N
    // renders from N.
    if (/^\s*\d+\.\s+/.test(line)) {
      const startNum = parseInt(line.match(/^\s*(\d+)\./)?.[1] ?? '1', 10) || 1;
      const items: string[] = [];
      while (i < lines.length) {
        if (/^\s*\d+\.\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
          i++;
        } else if (lines[i].trim() === '' && /^\s*\d+\.\s+/.test(lines[i + 1] ?? '')) {
          i++;                       // skip a blank line BETWEEN numbered items
        } else {
          break;
        }
      }
      out.push(
        <ol key={`ol-${k++}`} start={startNum}>
          {/* key=index: immutable parsed items, may duplicate, never reorder. (S6479 exception) */}
          {items.map((it, j) => <li key={j}>{renderInline(it, `oli-${k}-${j}`)}</li>)}
        </ol>,
      );
      continue;
    }

    // unordered list (- or *)
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ''));
        i++;
      }
      out.push(
        <ul key={`ul-${k++}`}>
          {/* key=index: immutable parsed items, may duplicate, never reorder. (S6479 exception) */}
          {items.map((it, j) => <li key={j}>{renderInline(it, `li-${k}-${j}`)}</li>)}
        </ul>,
      );
      continue;
    }

    // blank line → paragraph break
    if (!line.trim()) { i++; continue; }

    // paragraph: gather until a blank line or a block starter
    const pLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*```/.test(lines[i]) &&
      !/^#{1,6}\s/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) &&
      !/^\s*[-*]\s+/.test(lines[i])
    ) {
      pLines.push(lines[i]);
      i++;
    }
    // Preserve intentional soft line breaks inside a paragraph (the
    // container is no longer white-space:pre-wrap) while still running each
    // line through the inline tokenizer.
    out.push(
      <p key={`para-${k++}`}>
        {/* key=index: soft-wrapped lines of one immutable paragraph; positional,
            may duplicate, never reorder. (S6479 exception) */}
        {pLines.map((pl, j) => (
          <React.Fragment key={j}>
            {j > 0 && <br />}
            {renderInline(pl, `p-${k}-${j}`)}
          </React.Fragment>
        ))}
      </p>,
    );
  }
  return <>{out}</>;
}
