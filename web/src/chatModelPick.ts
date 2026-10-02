/** Which model the picker shows when the chat opens.
 *
 *  The model you chose stays chosen until YOU change it: the server's saved
 *  choice wins, whether or not that model is loaded right now. Only when
 *  nothing was ever saved does it fall back to this browser's last pick, then
 *  to the first model offered. It never saves anything by itself. */
export function pickChatModel(
  saved: string | null | undefined,
  offered: { id: string }[],
  browserLast: string,
): string {
  if (saved) return saved;
  if (browserLast && offered.some(m => m.id === browserLast)) return browserLast;
  return offered[0]?.id ?? '';
}
