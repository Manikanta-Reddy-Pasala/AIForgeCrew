import { followAfterScroll } from '../src/chatFollow.ts';

function assert(cond: boolean, msg: string): void {
  if (!cond) throw new Error(msg);
}

// A log 2000 tall in a 500 window: the end is at top = 1500.
const H = 2000, C = 500;

// Moving up lets go at once — also by a few pixels, also right at the end.
assert(followAfterScroll(true, 1500, 1490, H, C) === false, 'a small move up lets go');
assert(followAfterScroll(true, 1500, 0, H, C) === false, 'a jump to the top lets go');

// Once let go, it stays let go while the reader is anywhere above the end.
assert(followAfterScroll(false, 0, 300, H, C) === false, 'moving down, still far from the end');
assert(followAfterScroll(false, 300, 300, H, C) === false, 'no move at all');

// Coming back down to the end picks the following up again.
assert(followAfterScroll(false, 1200, 1470, H, C) === true, 'back near the end follows again');
assert(followAfterScroll(false, 1200, 1500, H, C) === true, 'back at the end follows again');

// A small nudge up near the end stays let go when the next line arrives: it
// takes a move down to follow again, not just being close.
assert(followAfterScroll(false, 1490, 1490, H, C) === false, 'near the end, no move, stays let go');
assert(followAfterScroll(false, 1480, 1470, H, C) === false, 'near the end, still moving up');
assert(followAfterScroll(false, null, 1490, H, C) === false, 'new log near the end stays let go');

// Following, and new text made the log longer under a reader who did not move.
assert(followAfterScroll(true, 1500, 1500, H + 300, C) === true, 'longer log keeps following');
assert(followAfterScroll(true, 200, 600, H, C) === true, 'moving down keeps following');

// A position that is a fraction off the end (browser zoom) is still the end.
assert(followAfterScroll(true, 1499.6, 1499.2, H, C) === true, 'a fraction off the end');

// A log shorter than its window has nowhere to scroll: it follows.
assert(followAfterScroll(false, 0, 0, 300, C) === true, 'short log follows');

// The log got shorter and the window was pushed up with it: still at the end.
assert(followAfterScroll(true, 1500, 1300, 1800, C) === true, 'a shorter log, still at the end');

// A log that was just rebuilt has no earlier position: nothing to compare.
assert(followAfterScroll(true, null, 0, H, C) === true, 'new log keeps following');
assert(followAfterScroll(false, null, 0, H, C) === false, 'new log stays let go');

console.log('chat-follow ok');
