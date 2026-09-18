// A small, SAFE markdown renderer for model text: everything is HTML-escaped
// first, then a handful of constructs are re-introduced (fences, `code`,
// **bold**, *italic*, headings, lists, http(s) links). Model output is
// untrusted — it can quote a web page or a ticket — so no raw HTML ever
// reaches the DOM.

export function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Inline code is set aside behind private-use placeholders (never NUL: an
// HTML parser turns NUL into U+FFFD and the code spans came back as digits).
function inline(s: string): string {
  const codes: string[] = [];
  let out = s.replace(/`([^`\n]+)`/g, (_m, c: string) => {
    codes.push(`<code>${c}</code>`);
    return `\uE000${codes.length - 1}\uE001`;
  });
  out = out
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2">$1</a>');
  return out.replace(/\uE000(\d+)\uE001/g, (_m, i: string) => codes[Number(i)]);
}

// Lists nest by indentation, and bullets right after a numbered item belong to
// it even unindented ("2. Fixes:" / "- a") — the same rules as the web UI's
// mdlite, so a sub-point never renders flush with the numbers.
const ITEM_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const indentOf = (ws: string) => ws.replace(/\t/g, '    ').length;

function parseItem(line: string) {
  const m = ITEM_RE.exec(line);
  if (!m) return null;
  const ordered = /\d/.test(m[2]);
  return { indent: indentOf(m[1]), ordered, num: ordered ? parseInt(m[2], 10) || 1 : 1, text: m[3] };
}

function renderList(lines: string[], i: number, base: number, ordered: boolean): { html: string; next: number } {
  const first = parseItem(lines[i])!;
  const items: string[][] = [];          // per item: [text html, …children html]
  let j = i;
  while (j < lines.length) {
    const line = lines[j];
    if (!line.trim()) {
      let n = j;
      while (n < lines.length && !lines[n].trim()) n++;
      const it = n < lines.length ? parseItem(lines[n]) : null;
      if (!it || !(it.indent > base || (it.indent === base && it.ordered === ordered))) break;
      j = n;
      continue;
    }
    const it = parseItem(line);
    const last = items[items.length - 1];
    if (it && it.indent === base && it.ordered === ordered) {
      items.push([inline(it.text)]);
      j++;
    } else if (it && last && (it.indent > base || (ordered && !it.ordered && it.indent === base))) {
      const sub = renderList(lines, j, it.indent, it.ordered);
      last.push(sub.html);
      j = sub.next;
    } else if (!it && last && indentOf(/^\s*/.exec(line)![0]) > base) {
      last[0] += '<br>' + inline(line.trim());
      j++;
    } else {
      break;
    }
  }
  const tag = ordered ? 'ol' : 'ul';
  const start = ordered && first.num !== 1 ? ` start="${first.num}"` : '';
  return { html: `<${tag}${start}>${items.map(p => `<li>${p.join('')}</li>`).join('')}</${tag}>`, next: j };
}

export function renderMarkdown(src: string): string {
  const lines = escapeHtml(src.replace(/\r\n?/g, '\n')).split('\n');
  const html: string[] = [];
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) html.push(`<p>${para.map(inline).join('<br>')}</p>`);
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = /^\s*```(\S*)\s*$/.exec(line);
    if (fence) {
      flushPara();
      const body: string[] = [];
      for (i++; i < lines.length && !/^\s*```\s*$/.test(lines[i]); i++) body.push(lines[i]);
      html.push(`<pre><code>${body.join('\n')}</code></pre>`);
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      flushPara();
      const lvl = Math.min(h[1].length + 2, 6);          // keep headings small in a sidebar
      html.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`);
      continue;
    }
    const it = parseItem(line);
    if (it) {
      flushPara();
      const r = renderList(lines, i, it.indent, it.ordered);
      html.push(r.html);
      i = r.next - 1;
      continue;
    }
    if (!line.trim()) { flushPara(); continue; }
    para.push(line);
  }
  flushPara();
  return html.join('');
}
