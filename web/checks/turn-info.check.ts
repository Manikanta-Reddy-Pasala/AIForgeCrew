import { fmtSecs, planFacts, stages, stopReason, usageLine } from '../src/turnInfo.ts';

function eq(got: unknown, want: unknown, msg: string): void {
  if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`${msg}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

// Why it stopped: the loop's specific reason beats the save's generic marker.
eq(stopReason([{ type: 'tool' }]), '', 'not stopped');
eq(stopReason([{ type: 'stopped', reason: 'llm_request_fails', error: 'HTTP 400  context\nlength' },
               { type: 'stopped', reason: 'guard' }]),
   'Stopped: the model kept failing — HTTP 400 context length', 'specific reason wins');
eq(stopReason([{ type: 'stopped', reason: 'cancelled' }]), '', 'the save marker alone adds nothing');
eq(stopReason([{ type: 'stopped', reason: 'guard' }]), '', 'guard alone adds nothing');
eq(stopReason([{ type: 'stopped', reason: 'brand_new' }]), 'Stopped: brand new', 'unknown reason');
eq(stopReason([{ type: 'stopped' }]), '', 'no reason');

// Usage line, from the saved ⚡ thought.
eq(usageLine([{ type: 'thought', text: '⚡ 12 LLM requests for this message (40 in this chat, 3.1k tokens written)' }]),
   '⚡ 12 LLM requests (40 in this chat, 3.1k tokens written)', 'usage');
eq(usageLine([{ type: 'thought', text: 'thinking about it' }]), '', 'no usage');

// Team stages, in order; subtask slugs and the harness are not stages; one agent is no strip.
eq(stages([{ role: 'triage' }, { role: 'planner' }, { role: 'system' }, { role: 'doer' }, { role: 'planner' }, { role: 'add-coupons' }]),
   ['triage', 'planner', 'doer'], 'team stages');
eq(stages([{ role: 'enhancer' }, {}, { role: undefined }]), [], 'simple turn: no strip');
eq(stages([{ role: 'Planner' }, { role: 'planner ' }, { role: 'Doer' }]), ['planner', 'doer'], 'case and spaces');

// Durations.
eq(fmtSecs(0.42), '0.4s', 'short'); eq(fmtSecs(12.6), '13s', 'seconds'); eq(fmtSecs(75), '1m 15s', 'minutes');
eq(fmtSecs(119.7), '2m 0s', 'rounds before splitting'); eq(fmtSecs(59.6), '1m 0s', 'just under a minute'); eq(fmtSecs(9.97), '10s', 'just under ten');

// Plan summary.
eq(planFacts('1. Add `shopkit/coupons.py`\n2. Wire coupons into orders.py\n3. Tests in tests/test_coupons.py\n- update README.md\n- storage.py'),
   { short: '5 steps · 5 files', files: ['coupons.py', 'orders.py', 'test_coupons.py', 'README.md', 'storage.py'] }, 'plan facts');
eq(planFacts('1. Install Node.js\n   - then npm i\n2. Edit web/app.js'), { short: '2 steps · 1 file', files: ['app.js'] }, 'products and nested bullets');
eq(planFacts('Just do it.'), { short: '', files: [] }, 'nothing to say');

console.log('turn-info ok');
