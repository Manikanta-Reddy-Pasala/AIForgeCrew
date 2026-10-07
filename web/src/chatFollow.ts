/** How close to the end of the log counts as "back at the end". */
export const FOLLOW_SLACK_PX = 40;

/** Should the chat log keep following new text after this scroll?
 *
 *  The reader owns the scroll position. Any move up lets go, and the log then
 *  stays where the reader put it while text keeps arriving. Coming back down to
 *  the end picks the following up again — it takes a move down to do that, so
 *  a reader who nudged up a little is not pulled back by the next line.
 *
 *  `prevTop` is null when there is no earlier position to compare with (the
 *  log was just rebuilt). */
export function followAfterScroll(
  following: boolean,
  prevTop: number | null,
  top: number,
  scrollHeight: number,
  clientHeight: number,
): boolean {
  const gap = scrollHeight - top - clientHeight;
  if (gap <= 2) return true;
  if (prevTop !== null && top < prevTop) return false;
  if (prevTop !== null && top > prevTop && gap <= FOLLOW_SLACK_PX) return true;
  return following;
}
