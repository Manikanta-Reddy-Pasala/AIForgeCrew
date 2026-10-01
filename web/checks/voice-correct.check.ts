import { applySpokenCorrection, planSpokenInsert, type SpokenAnchor } from '../src/voicePhrase.ts';

function assert(cond: boolean, msg: string): void {
  if (!cond) throw new Error(msg);
}

const first = planSpokenInsert('', 'save the file');
assert(first.text === 'save the file' && first.start === 0, 'first phrase');
const second = planSpokenInsert(first.text, 'add the invoice button');
assert(second.start === 'save the file '.length, 'second phrase starts after a space');
assert(second.text === 'save the file add the invoice button', 'both phrases kept');

const early: SpokenAnchor = { start: first.start, raw: 'save the file', epoch: 1 };
const later: SpokenAnchor = { start: second.start, raw: 'add the invoice button', epoch: 1 };
const anchors = [early, later];
const tidied = applySpokenCorrection(second.text, anchors, early, 'Save the file.', 1);
assert(tidied === 'Save the file. add the invoice button', 'only the dictated phrase changes');
assert(later.start === 'Save the file. '.length, 'the next phrase slides with the length change');
const tidiedLater = applySpokenCorrection(tidied, anchors, later, 'Add the invoice button.', 1);
assert(tidiedLater === 'Save the file. Add the invoice button.', 'the shifted phrase still matches');

const typed = planSpokenInsert('save the file please', 'save the file');
const dictated: SpokenAnchor = { start: typed.start, raw: 'save the file', epoch: 1 };
const onlyLatest = applySpokenCorrection(
  typed.text, [dictated], dictated, 'Save the file.', 1);
assert(onlyLatest === 'save the file please Save the file.', 'an earlier typed copy stays');

const stale: SpokenAnchor = { start: 0, raw: 'fix the login', epoch: 1 };
assert(
  applySpokenCorrection('fix the login bug', [], stale, 'Fix the login.', 2) === 'fix the login bug',
  'a correction from the previous draft is ignored',
);
assert(
  applySpokenCorrection('fix the loginX', [], { start: 0, raw: 'fix the login', epoch: 1 }, 'Fix the login.', 1)
    === 'fix the loginX',
  'an edited phrase is left alone',
);

console.log('voice-correct ok');
