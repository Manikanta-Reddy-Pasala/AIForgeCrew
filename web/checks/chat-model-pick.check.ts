import { pickChatModel } from '../src/chatModelPick.ts';

function assert(cond: boolean, msg: string): void {
  if (!cond) throw new Error(msg);
}

const offered = [{ id: 'small-fast' }, { id: 'big-coder' }];

// The saved choice wins — also when it is not the first, and when the server
// did not list it this time (not loaded, unreachable): it is never replaced.
assert(pickChatModel('big-coder', offered, 'small-fast') === 'big-coder', 'saved choice wins');
assert(pickChatModel('not-loaded-now', offered, 'small-fast') === 'not-loaded-now',
  'a model that is not listed right now is still the chosen one');
assert(pickChatModel('big-coder', [], '') === 'big-coder', 'kept with an empty list');

// Nothing saved yet: this browser's last pick, if still offered, else the first.
assert(pickChatModel('', offered, 'big-coder') === 'big-coder', 'browser pick when nothing saved');
assert(pickChatModel(null, offered, 'gone-model') === 'small-fast', 'first offered as last resort');
assert(pickChatModel(undefined, [], '') === '', 'nothing to pick');

console.log('chat-model-pick ok');
