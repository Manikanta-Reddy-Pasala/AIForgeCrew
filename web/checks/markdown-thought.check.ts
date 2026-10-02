import { looksLikeMarkdown } from '../src/looksLikeMarkdown.ts';

function assert(cond: boolean, msg: string): void {
  if (!cond) throw new Error(msg);
}

// The answer from the report: headings, a table, bold, code spans.
assert(looksLikeMarkdown('## 1. Schema of the first ClickHouse table — **YES**'), 'heading + bold');
assert(looksLikeMarkdown('| Column | Type |\n|---|---|\n| id | UInt32 |'), 'table');
assert(looksLikeMarkdown('see `deploy/clickhouse_init/01_pdw.sql` for it'), 'code span');
assert(looksLikeMarkdown('Steps:\n- first\n- second'), 'bullet list');
assert(looksLikeMarkdown('1. one\n2. two'), 'numbered list');
assert(looksLikeMarkdown('```py\nprint(1)\n```'), 'fence');

// Plain prose stays plain (an italic thought row).
assert(!looksLikeMarkdown('I will read the file first and then run the tests.'), 'prose');
assert(!looksLikeMarkdown('Running the checks... 3 of 5 done.'), 'prose with numbers');
assert(!looksLikeMarkdown(''), 'empty');

console.log('markdown-thought ok');
