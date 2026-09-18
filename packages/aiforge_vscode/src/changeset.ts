// Which files the agent changed this chat, from its events — pure, so the tree
// view and the tests share it. Two sources:
//  * a finished file-writing tool call (``tool`` event) names its path, so the
//    file shows up the moment it is written;
//  * a ``changes`` event (end of a run) carries every changed file with its
//    status, +/- counts and unified diff, which then wins.
import { AgentEvent } from './sse';
import { isInternalPath, writtenPath } from './fileTools';
import { reportedToHost } from './paths';

export type ChangedFile = {
  hostPath: string;       // absolute, on this machine
  label: string;          // as the agent named it (relative when it can be)
  status: string;         // added / modified / deleted / changed / written
  additions?: number;
  deletions?: number;
  patch?: string;         // unified diff from a `changes` event
};

export class ChangeSet {
  private files = new Map<string, ChangedFile>();

  constructor(private boxCwd: string, private hostCwd: string,
              private platform: string = process.platform) {}

  /** Record what ``ev`` says changed; true when the set changed. */
  apply(ev: AgentEvent): boolean {
    if (ev.type === 'changes' && Array.isArray(ev.files)) {
      for (const f of ev.files as any[]) {
        if (!f || typeof f.path !== 'string' || isInternalPath(f.path)) continue;
        const hostPath = reportedToHost(f.path, this.boxCwd, this.hostCwd, this.platform);
        this.files.set(hostPath, {
          hostPath, label: f.path, status: String(f.status || 'changed'),
          additions: num(f.additions), deletions: num(f.deletions),
          patch: typeof f.diff === 'string' && f.diff ? f.diff : undefined,
        });
      }
      return true;
    }
    const reported = ev.type === 'tool' ? writtenPath(ev) : null;
    if (reported && !isInternalPath(reported)) {
      const hostPath = reportedToHost(reported, this.boxCwd, this.hostCwd, this.platform);
      if (this.files.has(hostPath)) return false;       // keep the richer entry
      this.files.set(hostPath, { hostPath, label: reported, status: 'written' });
      return true;
    }
    return false;
  }

  hostPathOf(reported: string): string {
    return reportedToHost(reported, this.boxCwd, this.hostCwd, this.platform);
  }

  list(): ChangedFile[] {
    return [...this.files.values()].sort((a, b) => a.label.localeCompare(b.label));
  }

  clear(): void { this.files.clear(); }
}

const num = (v: unknown) => (typeof v === 'number' ? v : undefined);
