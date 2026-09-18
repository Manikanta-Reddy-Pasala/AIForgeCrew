// Which tool calls write a file — shared by the extension (changed-files tree)
// and the chat webview (per-turn change cards), so both agree. Mirrors
// aiforge_core/runtime/tools/mutating.py FILE_WRITE_TOOLS; ``editor``
// multiplexes reads on the same name.

export const FILE_WRITE_TOOLS = new Set([
  'file_write', 'file_create', 'file_patch', 'editor', 'multi_edit', 'rename_symbol',
  'format', 'write', 'write_file', 'create_file', 'patch', 'apply_patch', 'edit',
  'edit_block', 'str_replace',
]);
const EDITOR_READONLY = new Set(['view', 'read', 'list', 'ls', 'cat', 'open']);

type ToolLike = { [key: string]: unknown };

/** The path a successful file-writing call wrote, or null. */
export function writtenPath(t: ToolLike): string | null {
  const name = String(t.name || '');
  const args = (t.args || {}) as Record<string, unknown>;
  if (!FILE_WRITE_TOOLS.has(name) || typeof args.path !== 'string' || !args.path) return null;
  const sub = String(args.command ?? args.sub_command ?? '').toLowerCase();   // as mutating.py
  if (name === 'editor' && EDITOR_READONLY.has(sub)) return null;
  const result = (t.result || {}) as Record<string, unknown>;
  if (result.ok === false || result.error || result.blocked) return null;
  return args.path;
}

/** AIForge's own bookkeeping (the code-graph index), not the user's work. */
export function isInternalPath(p: string): boolean {
  return /(^|\/)\.codegraph(\/|$)/.test(p);
}
