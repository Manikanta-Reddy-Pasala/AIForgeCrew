// Host folder ↔ sandbox path. The same rules as the CLI (aiforge_cli/paths.py):
// on macOS and Linux a mounted folder has the SAME path inside the box; on
// Windows `C:\work\app` is `/host/c/work/app`. macOS (APFS) and Windows compare
// case-insensitively.
import * as path from 'node:path';

export const BOX_PREFIX = '/host';

const isWindows = (platform: string) => platform === 'win32';

export function toBox(hostPath: string, platform: string = process.platform): string {
  if (!isWindows(platform)) return hostPath;
  const m = /^([A-Za-z]):[\\/]*(.*)$/.exec(hostPath);
  if (!m) return hostPath;
  const rest = m[2].replace(/\\/g, '/').replace(/\/+$/, '');
  return path.posix.join(BOX_PREFIX, m[1].toLowerCase(), rest);
}

export function toHost(boxPath: string, platform: string = process.platform): string {
  if (!isWindows(platform) || !boxPath.startsWith(BOX_PREFIX + '/')) return boxPath;
  const [drive, ...rest] = boxPath.slice(BOX_PREFIX.length + 1).split('/');
  return `${drive.toUpperCase()}:\\${rest.join('\\')}`;
}

function caseInsensitive(platform: string): boolean {
  return platform === 'win32' || platform === 'darwin';
}

/** Whether ``child`` is ``parent`` or inside it. Box paths are POSIX. */
export function within(child: string, parent: string, platform: string = process.platform): boolean {
  let c = child.replace(/\/+$/, '');
  let p = parent.replace(/\/+$/, '');
  if (caseInsensitive(platform)) { c = c.toLowerCase(); p = p.toLowerCase(); }
  return c === p || c.startsWith(p + '/') || (p === '' && c.startsWith('/'));
}

/** The mounted folder containing ``boxPath`` — the LONGEST match, so a nested
 *  mount wins over its parent — or null when the box cannot see it. */
export function coveringMount(boxPath: string, mounts: string[],
                              platform: string = process.platform): string | null {
  const hits = mounts.filter(m => within(boxPath, m, platform));
  return hits.length ? hits.reduce((a, b) => (b.length > a.length ? b : a)) : null;
}

/** A path the agent reported (absolute box path, or relative to the session's
 *  working folder) as an absolute HOST path. */
export function reportedToHost(reported: string, boxCwd: string, hostCwd: string,
                               platform: string = process.platform): string {
  const P = isWindows(platform) ? path.win32 : path.posix;
  if (reported.startsWith('/')) {
    if (within(reported, boxCwd, platform)) {
      const rel = reported.slice(boxCwd.replace(/\/+$/, '').length).replace(/^\/+/, '');
      return rel ? P.join(hostCwd, ...rel.split('/')) : hostCwd;
    }
    return toHost(reported, platform);
  }
  return P.join(hostCwd, ...reported.split('/'));
}
