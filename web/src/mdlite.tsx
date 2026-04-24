/* mdlite — a tiny markdown-ish renderer for chat answers.
 * Handles: fenced ```code``` blocks, inline `code`, `- `/`* ` bullet lists,
 * blank-line paragraphs. No tables, no links beyond bare URLs which stay
 * as plain text. Designed to stay under 60 lines and produce React nodes.
 */
import React from 'react';

function renderInline(text: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  let i = 0;
  let buf = '';
  let n = 0;
  while (i < text.length) {
    if (text[i] === '`') {
      if (buf) { nodes.push(buf); buf = ''; }
      const end = text.indexOf('`', i + 1);
      if (end === -1) { buf += text[i]; i++; continue; }
      nodes.push(<code key={`${keyPrefix}-c-${n++}`}>{text.slice(i + 1, end)}</code>);
      i = end + 1;
    } else {
      buf += text[i++];
    }
  }
  if (buf) nodes.push(buf);
  return nodes;
}

export function MdLite({ text }: { text: string }) {
  if (!text) return null;
  const out: React.ReactNode[] = [];
  const lines = text.split('\n');
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i];
    // fenced code
    if (/^```/.test(line)) {
      const fenceEnd = lines.findIndex((l, j) => j > i && /^```/.test(l));
      const end = fenceEnd === -1 ? lines.length : fenceEnd;
      const body = lines.slice(i + 1, end).join('\n');
      out.push(<pre key={`p-${k++}`}><code>{body}</code></pre>);
      i = end + 1;
      continue;
    }
    // list block
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ''));
        i++;
      }
      out.push(
        <ul key={`ul-${k++}`}>
          {items.map((it, j) => (
            <li key={j}>{renderInline(it, `li-${k}-${j}`)}</li>
          ))}
        </ul>,
      );
      continue;
    }
    // blank line → paragraph break (flush by appending a <p/>)
    if (!line.trim()) {
      i++;
      continue;
    }
    // paragraph: gather until blank or list/fence
    const pLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^```/.test(lines[i]) &&
      !/^\s*[-*]\s+/.test(lines[i])
    ) {
      pLines.push(lines[i]);
      i++;
    }
    out.push(<p key={`para-${k++}`}>{renderInline(pLines.join(' '), `p-${k}`)}</p>);
  }
  return <>{out}</>;
}
