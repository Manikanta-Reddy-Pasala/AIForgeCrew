// A unified diff as coloured rows (the `changes` event carries one per file).
import { escapeHtml } from './md';

export function renderDiff(diff: string): string {
  const rows = diff.replace(/\r\n?/g, '\n').split('\n')
    .filter(l => !/^(diff --git|index |--- |\+\+\+ |new file mode|deleted file mode)/.test(l))
    .map(l => {
      const cls = l.startsWith('@@') ? 'hunk' : l.startsWith('+') ? 'add'
        : l.startsWith('-') ? 'del' : 'ctx';
      return `<div class="dl ${cls}">${escapeHtml(l) || '&nbsp;'}</div>`;
    });
  return `<div class="diff">${rows.join('')}</div>`;
}
