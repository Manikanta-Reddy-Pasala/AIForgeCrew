/** Headings, lists, tables, bold, code spans or fences: more than prose. */
export function looksLikeMarkdown(text: string): boolean {
  return /(^|\n)\s*(#{1,4}\s|[-*]\s+\S|\d+\.\s+\S|\|.+\|)|\*\*[^*]+\*\*|```|`[^`\n]+`/.test(text || '');
}
