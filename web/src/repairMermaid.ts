/* Make a flowchart drawable when a node label contains raw parentheses.
 *
 * Mermaid treats `(` inside an unquoted `[]` or `{}` label as the start of
 * another shape, so `A[step (detail)]` and `B{check (detail)}` fail to parse.
 * Quoted labels are the legal form. Already-quoted text, `-- label -->` edge
 * text, cylinders `id[(text)]`, stadiums `id([text])`, and slash shapes
 * `id[/text/]` are left as written. Other diagram types are unchanged.
 */

const SQUARE = /(?<![(\[])([A-Za-z_][\w]*)\[(?![(\[\\/])([^\]\n]*)\]/g;
const DIAMOND = /(?<!\{)([A-Za-z_][\w]*)\{(?![{])([^}\n]*)\}/g;
const QUOTED_SPAN = /"[^"\n]*"/g;
const EDGE_LABEL = /--(?!>)([^>\n]*?)(?=-->)/g;

function isFlowchart(source: string): boolean {
  for (const line of source.split('\n')) {
    const text = line.trim();
    if (!text || text.startsWith('%%')) continue;
    return /^(flowchart|graph)\b/i.test(text);
  }
  return false;
}

function quoted(text: string): string {
  const body = text.replace(/\\n/g, '<br/>').replace(/"/g, '#quot;');
  return `"${body}"`;
}

function hold(source: string, held: string[], pattern: RegExp): string {
  pattern.lastIndex = 0;
  return source.replace(pattern, (whole) => {
    held.push(whole);
    return `\x00Q${held.length - 1}\x00`;
  });
}

function restore(source: string, held: string[]): string {
  let out = source;
  for (let i = held.length - 1; i >= 0; i--) {
    out = out.replace(`\x00Q${i}\x00`, held[i]);
  }
  return out;
}

function quoteRisky(source: string, pattern: RegExp, wrap: (id: string, text: string) => string): string {
  pattern.lastIndex = 0;
  return source.replace(pattern, (whole, id: string, text: string) => {
    if (!text.includes('(') && !text.includes(')')) return whole;
    return wrap(id, quoted(text));
  });
}

/** A flowchart Mermaid can parse, or the original text when nothing was unsafe. */
export function repairMermaid(source: string): string {
  if (!isFlowchart(source)) return source;
  // A backslash before `:::` is not class syntax; it shows up when a fence is
  // escaped in transit and then fails the next token after the label is fixed.
  let out = source.replace(/\\+:::/g, ':::');
  const held: string[] = [];
  out = hold(out, held, QUOTED_SPAN);
  out = hold(out, held, EDGE_LABEL);
  out = quoteRisky(out, SQUARE, (id, text) => `${id}[${text}]`);
  out = quoteRisky(out, DIAMOND, (id, text) => `${id}{${text}}`);
  return restore(out, held);
}
