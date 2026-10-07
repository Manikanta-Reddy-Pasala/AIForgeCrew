/** What a tool step is doing, in plain words, from its name and arguments.
 *
 *  The step row shows the raw call (`run_command(cmd=pytest -q …)`), which
 *  says exactly what ran but not what it is for. This line goes above it:
 *  "Running the tests", "Reading shopkit/models.py", "Saving the changes as
 *  a commit". It is worked out here, from the call itself — no model call,
 *  nothing to wait for, and it works on chats saved before it existed. A
 *  call it has no words for gets a generic line built from the tool's name. */

type Args = Record<string, unknown>;

const s = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v));

/** A path as people say it: the last two parts, so `models.py` keeps its folder. */
function short(path: unknown): string {
  const raw = s(path).trim();
  if (raw === '/' || raw === '\\') return raw;
  const p = raw.replace(/[\\/]+$/, '');
  if (!p) return '';
  const parts = p.split(/[\\/]/).filter(Boolean);
  return parts.length <= 2 ? parts.join('/') || p : '…/' + parts.slice(-2).join('/');
}

function quote(text: unknown, max = 40): string {
  const t = s(text).replace(/\s+/g, ' ').trim();
  return t.length > max ? `"${t.slice(0, max - 1)}…"` : `"${t}"`;
}

function list(paths: unknown): string {
  const ps = Array.isArray(paths) ? paths.map(short).filter(Boolean) : [];
  if (ps.length === 0) return 'files';
  if (ps.length === 1) return ps[0];
  if (ps.length === 2) return `${ps[0]} and ${ps[1]}`;
  return `${ps[0]}, ${ps[1]} and ${ps.length - 2} more`;
}

function editsTarget(edits: unknown): string {
  const ps = Array.isArray(edits)
    ? [...new Set(edits.map(e => s((e as Args)?.path)).filter(Boolean))]
    : [];
  return ps.length ? list(ps) : 'files';
}

/** Words of a command line, quoted words kept whole and unquoted. The shell
 *  operators come out as words of their own, also when written without
 *  spaces (`a&&b`, `x>out`). */
function words(cmd: string): string[] {
  const out: string[] = [];
  const re = /"((?:[^"\\]|\\.)*)"|'([^']*)'|(&&|\|\||<<-?|>>|[;|<>])|([^\s"';|&<>]+|&)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(cmd)) !== null) out.push(m[1] ?? m[2] ?? m[3] ?? m[4]);
  return out;
}

const OPS = new Set(['&&', '||', ';', '|']);
const VALUE_FLAGS = new Set(['-A', '-B', '-C', '-e', '-f', '-m', '-t', '-g', '-T',
  '--type', '--include', '--exclude', '--glob', '--max-count', '--context']);

/** The first piece of a chain that does something (a leading `cd` only
 *  changes folder), without its `FOO=1` and `sudo` prefixes. */
function firstCommand(w: string[]): string[] {
  const segs: string[][] = [[]];
  for (const t of w) {
    if (OPS.has(t)) segs.push([]);
    else segs[segs.length - 1].push(t);
  }
  let seg = segs.find(x => x.length && x[0] !== 'cd') ?? segs[0];
  while (seg.length && (/^[A-Za-z_][A-Za-z0-9_]*=/.test(seg[0]) || seg[0] === 'sudo' || seg[0] === 'time')) {
    seg = seg.slice(1);
  }
  return seg;
}

/** Where a `>` / `>>` in the command writes to, if anywhere. */
function writesTo(w: string[]): string | null {
  for (let i = 0; i < w.length - 1; i++) {
    if ((w[i] === '>' || w[i] === '>>') && w[i + 1] !== '&' && !/^&?\d$/.test(w[i + 1])
        && w[i + 1] !== '/dev/null') return w[i + 1];
  }
  return null;
}

const TEST_TOOLS = new Set(['pytest', 'py.test', 'jest', 'vitest', 'mocha', 'tox', 'nox', 'phpunit', 'rspec']);

function isTestRun(w: string[]): boolean {
  const [h, a1, a2] = w;
  const tool = (h ?? '').replace(/^\.\//, '');
  if (TEST_TOOLS.has(tool)) return true;
  if (/^python3?$/.test(tool) && a1 === '-m' && (a2 === 'pytest' || a2 === 'unittest')) return true;
  if (/^(npm|yarn|pnpm|bun)$/.test(tool) && (a1 === 'test' || (a1 === 'run' && /^test(:|$)/.test(a2 ?? '')))) return true;
  if ((tool === 'go' || tool === 'cargo') && a1 === 'test') return true;
  if (/^(mvn|mvnw|gradle|gradlew)$/.test(tool) && w.slice(1).some(x => x === 'test' || x === 'verify')) return true;
  return false;
}

/** A launcher in front of the real command (`npx jest`, `uv run pytest`). */
function unwrap(w: string[]): string[] | null {
  const [h, a1] = w;
  if (h === 'npx' || h === 'bunx' || h === 'pnpx') return w.slice(1).filter((x, i) => i > 0 || !x.startsWith('-'));
  if (/^(uv|poetry|pipenv|pdm|hatch|rye)$/.test(h ?? '') && a1 === 'run') return w.slice(2);
  if (h === 'env') return w.slice(1).filter(x => !/^[A-Za-z_][A-Za-z0-9_]*=/.test(x));
  return null;
}

function firstPlain(w: string[], from = 1): string | undefined {
  for (let i = from; i < w.length; i++) {
    const t = w[i];
    if (VALUE_FLAGS.has(t)) { i++; continue; }
    if (t.startsWith('-')) continue;
    return t;
  }
  return undefined;
}

function describeGit(w: string[]): string {
  let i = 1;
  while (i < w.length && w[i].startsWith('-')) {
    i += (w[i] === '-C' || w[i] === '-c') ? 2 : 1;      // git -C dir status
  }
  const sub = w[i] ?? '';
  const rest = w.slice(i + 1);
  const plain = rest.filter(x => !x.startsWith('-'));
  switch (sub) {
    case 'status': return 'Checking which files changed';
    case 'diff': return 'Looking at what changed in the code';
    case 'log': return 'Looking at the commit history';
    case 'show': return 'Looking at a commit';
    case 'add': return 'Staging files for the next commit';
    case 'commit': return 'Saving the changes as a commit';
    case 'push': return 'Sending the commits to the remote';
    case 'pull': return 'Pulling the latest changes';
    case 'fetch': return 'Fetching the latest changes';
    case 'checkout': case 'switch': {
      const nb = rest.findIndex(x => x === '-b' || x === '-B' || x === '-c' || x === '-C');
      if (nb >= 0 && rest[nb + 1]) return `Creating branch ${rest[nb + 1]}`;
      if (rest.includes('--') || plain[0] === '.') return 'Throwing away changes to files';
      return plain[0] ? `Switching to ${plain[0]}` : 'Switching branch';
    }
    case 'restore': case 'reset': case 'clean': return 'Throwing away changes';
    case 'branch':
      if (rest.some(x => x === '-d' || x === '-D' || x === '--delete')) return `Deleting branch ${plain[0] ?? ''}`.trim();
      return plain[0] ? `Creating branch ${plain[0]}` : 'Looking at the branches';
    case 'merge': return 'Merging branches';
    case 'rebase': return 'Rebasing the branch';
    case 'stash':
      if (plain[0] === 'pop' || plain[0] === 'apply') return 'Bringing back set-aside changes';
      if (plain[0] === 'list' || plain[0] === 'show') return 'Looking at set-aside changes';
      return 'Setting changes aside';
    case 'clone': return 'Copying a repository';
    case 'grep': return `Searching the code for ${quote(firstPlain(w, i + 1) ?? '')}`;
    case 'worktree': return 'Managing git worktrees';
    case 'tag': return 'Tagging a release';
    case '': return 'Running git';
    default: return `Running git ${sub}`;
  }
}

/** One shell command, read for its intent. */
export function describeCommand(cmd: string, depth = 0): string {
  const all = words(s(cmd).slice(0, 4000));
  const w = firstCommand(all);
  const head = (w[0] ?? '').replace(/^\.\//, './');
  const arg = (i: number) => w[i] ?? '';
  if (!head) return 'Running a command';

  // `bash -lc "pytest -q"`: the quoted command is what runs.
  if (depth < 2 && /^(bash|sh|zsh)$/.test(head) && w.some(x => /^-\w*c$/.test(x))) {
    const inner = w[w.findIndex(x => /^-\w*c$/.test(x)) + 1];
    if (inner) return describeCommand(inner, depth + 1);
  }
  const un = depth < 2 ? unwrap(w) : null;
  if (un && un.length) return describeCommand(un.join(' '), depth + 1);

  const out = writesTo(w);
  if (out) return `Writing ${short(out)}`;
  if (head === 'git') return describeGit(w);
  if (isTestRun(w)) return 'Running the tests';

  if (['cat', 'head', 'tail', 'less', 'more', 'nl', 'bat'].includes(head)) {
    const file = w.slice(1).filter(x => !x.startsWith('-') && !/^\d+$/.test(x) && x !== '<').pop();
    return file ? `Reading ${short(file)}` : 'Reading a file';
  }
  if (head === 'sed' && (w.includes('-n') || w.some(x => /^-\w*n\w*$/.test(x))) && !w.some(x => /^-\w*i/.test(x))) {
    return `Reading part of ${short(w[w.length - 1])}`;
  }
  if (head === 'sed' || head === 'awk' || head === 'perl') return 'Editing text with a script';
  if (['grep', 'egrep', 'rg', 'ag', 'ack', 'ugrep'].includes(head)) {
    const e = w.indexOf('-e');
    const pat = e >= 0 ? w[e + 1] : firstPlain(w);
    return pat ? `Searching for ${quote(pat)}` : 'Searching the code';
  }
  if (head === 'find' || head === 'fd') return 'Looking for files';
  if (head === 'ls' || head === 'tree') {
    const dir = firstPlain(w);
    return dir ? `Listing ${short(dir)}` : 'Listing the files here';
  }
  if (head === 'wc') return 'Counting lines';
  if (head === 'mkdir') return `Creating folder ${short(w[w.length - 1])}`;
  if (head === 'rm' || head === 'rmdir') return 'Deleting files';
  if (head === 'mv') return 'Moving or renaming files';
  if (head === 'cp') return 'Copying files';
  if (head === 'touch') return 'Creating an empty file';
  if (head === 'chmod' || head === 'chown') return 'Changing file permissions';
  if (/^pip3?$/.test(head) && arg(1) === 'install') return 'Installing Python packages';
  if (/^python3?$/.test(head) && arg(1) === '-m' && /^pip3?$/.test(arg(2)) && arg(3) === 'install') return 'Installing Python packages';
  if ((head === 'uv' && (arg(1) === 'add' || arg(1) === 'sync' || (arg(1) === 'pip' && arg(2) === 'install')))
      || (head === 'poetry' && (arg(1) === 'add' || arg(1) === 'install'))) return 'Installing Python packages';
  if (/^(npm|yarn|pnpm|bun)$/.test(head)) {
    const a1 = arg(1);
    const script = a1 === 'run' ? arg(2) : a1;
    if (['install', 'i', 'add', 'ci'].includes(a1) || (head === 'yarn' && !a1)) return 'Installing packages';
    if (/^build/.test(script)) return 'Building the app';
    if (/^lint/.test(script)) return 'Checking the code style';
    if (/^(typecheck|tsc)/.test(script)) return 'Checking the types';
    if (/^(dev|start|serve|preview)/.test(script)) return 'Starting the app';
    return script ? `Running ${head} ${script}` : `Running ${head}`;
  }
  if (/^(mvn|mvnw|\.\/mvnw|gradle|gradlew|\.\/gradlew|make|cargo|go)$/.test(head)) {
    if (w.slice(1).some(x => /^(build|package|compile|install|assemble)$/.test(x))) return 'Building the project';
    return `Running ${head.replace(/^\.\//, '')} ${firstPlain(w) ?? ''}`.trim();
  }
  if (/^(tsc|mypy|pyright)$/.test(head)) return 'Checking the types';
  if (/^(ruff|flake8|pylint|eslint|black|prettier|isort)$/.test(head)) return 'Checking the code style';
  if (/^(python3?|node|ruby|deno|bash|sh|zsh)$/.test(head)) {
    if (w.includes('-c') || w.includes('-e')) return 'Running a short script';
    if (/^python3?$/.test(head) && arg(1) === '-m') return `Running ${arg(2)}`;
    const target = firstPlain(w);
    return target ? `Running ${short(target)}` : `Starting ${head}`;
  }
  if (head.startsWith('./')) return `Running ${short(head)}`;
  if (head === 'curl' || head === 'wget' || head === 'http') {
    const url = w.find(x => /^https?:\/\//.test(x));
    try { if (url) return `Calling ${new URL(url).host}`; } catch { /* below */ }
    return 'Calling a web address';
  }
  if (head === 'docker' || head === 'kubectl' || head === 'helm') return `Running ${head} ${firstPlain(w) ?? ''}`.trim();
  if (head === 'echo' || head === 'printf') return 'Printing a value';
  if (head === 'which' || head === 'type' || head === 'command') return 'Checking a tool is installed';
  if (head === 'pwd') return 'Checking the current folder';
  return 'Running a command';
}

/** The plain-words line for one tool step. */
export function describeStep(name: string, args: Args | undefined | null): string {
  const a: Args = args ?? {};
  switch (name) {
    case 'file_read': case 'read': return `Reading ${short(a.path) || 'a file'}`;
    case 'read_lines': {
      const end = Number(a.end) > 0 ? s(a.end) : 'end';
      return `Reading lines ${s(a.start) || '1'}–${end} of ${short(a.path) || 'a file'}`;
    }
    case 'read_files': return `Reading ${list(a.paths)}`;
    case 'file_write': return `Writing ${short(a.path) || 'a file'}`;
    case 'file_create': return `Creating ${short(a.path) || 'a file'}`;
    case 'file_patch': return `Editing ${short(a.path) || 'a file'}`;
    case 'multi_edit': return `Editing ${editsTarget(a.edits)}`;
    case 'editor': {
      const cmd = s(a.command);
      if (cmd === 'view') return `Reading ${short(a.path) || 'a file'}`;
      if (cmd === 'create') return `Creating ${short(a.path) || 'a file'}`;
      return `Editing ${short(a.path) || 'a file'}`;
    }
    case 'list_dir': return `Listing ${short(a.path) || 'the files here'}`;
    case 'find': return a.name ? `Looking for files named ${quote(a.name)}` : 'Looking for files';
    case 'grep': return a.pattern
      ? `Searching ${short(a.path) ? short(a.path) + ' ' : 'the code '}for ${quote(a.pattern)}` : 'Searching the code';
    case 'run_command': case 'bash': case 'shell': case 'run_shell': case 'run':
      return describeCommand(s(a.cmd ?? a.command));
    case 'command_wait': return 'Waiting for a command to finish';
    case 'command_output': return 'Checking on a running command';
    case 'command_kill': return 'Stopping a running command';
    case 'watch_until': return a.until ? `Watching until ${s(a.until).replace(/_/g, ' ')}` : 'Watching a command';
    case 'run_tests': return a.pattern ? `Running the tests matching ${quote(a.pattern)}` : 'Running the tests';
    case 'typecheck': return 'Checking the types';
    case 'format': return `Formatting ${short(a.path) || 'the code'}`;
    case 'rename_symbol': return a.old_name && a.new_name
      ? `Renaming ${s(a.old_name)} to ${s(a.new_name)}` : 'Renaming a name in the code';
    case 'lsp': return 'Asking the language server';
    case 'serve': return 'Starting a service';
    case 'stop_service': return 'Stopping a service';
    case 'git_status': return 'Checking which files changed';
    case 'git_diff': return `Looking at what changed${short(a.path) ? ' in ' + short(a.path) : ''}`;
    case 'git_log': return 'Looking at the commit history';
    case 'git_blame': return `Looking at who changed ${short(a.path) || 'the file'}`;
    case 'github_pr': return 'Opening a pull request';
    case 'gitlab_mr_create': return 'Opening a merge request';
    case 'codegraph_query': case 'codegraph_explore':
      return a.symbol || a.query ? `Looking up ${quote(a.symbol || a.query)} in the code map` : 'Looking at the code map';
    case 'codegraph_callers': return a.symbol ? `Finding what calls ${s(a.symbol)}` : 'Finding callers';
    case 'codegraph_callees': return a.symbol ? `Finding what ${s(a.symbol)} calls` : 'Finding what a function calls';
    case 'codegraph_impact': return a.symbol ? `Checking what a change to ${s(a.symbol)} would affect` : 'Checking what a change would affect';
    case 'memory_lookup':
      if (a.id) return 'Reading back saved text';
      return a.query ? `Searching memory for ${quote(a.query)}` : 'Searching memory';
    case 'memory_write': return 'Saving a note to memory';
    case 'search_chat_sessions': return a.query ? `Searching earlier chats for ${quote(a.query)}` : 'Searching earlier chats';
    case 'session_actions': return 'Looking at what this chat already ran';
    case 'remember_rule': return 'Saving a rule';
    case 'skill_search': return a.query ? `Looking for a skill for ${quote(a.query)}` : 'Looking for a skill';
    case 'workflow_search': return a.query ? `Looking for a workflow for ${quote(a.query)}` : 'Looking for a workflow';
    case 'workflow_run': return `Running the ${s(a.name) || 'saved'} workflow`;
    case 'web_fetch': case 'web_crawl': {
      try { return `Opening ${new URL(s(a.url)).host}`; } catch { return 'Opening a web page'; }
    }
    case 'browse': return 'Using the browser';
    case 'ui_check': return 'Taking a screenshot of the page';
    case 'spawn_task': return 'Starting a side task';
    case 'delegate': case 'delegate_to_agent': return 'Handing a task to another agent';
    case 'plan_progress': return `Marking "${s(a.title) || s(a.slug)}" as ${s(a.status) || 'updated'}`;
    case 'execute_ipython_cell': return 'Running Python code';
    case 'jira_read': case 'jira_comments': case 'jira_worklog': return `Reading Jira ${s(a.key)}`.trim();
    case 'jira_search': return 'Searching Jira';
    case 'jira_comment': return `Commenting on Jira ${s(a.key)}`.trim();
    case 'jira_create': return 'Creating a Jira issue';
    case 'jira_update': case 'jira_transition': case 'jira_assign': return `Updating Jira ${s(a.key)}`.trim();
    case 'confluence_read': return 'Reading a Confluence page';
    case 'confluence_search': return 'Searching Confluence';
    case 'confluence_create': return 'Creating a Confluence page';
    case 'confluence_update': return 'Updating a Confluence page';
    case 'email_send': return 'Sending an email';
    case 'email_read': return 'Reading email';
    case 'committed': return 'Saved the changes as a commit';
    case 'mcp': return `Using ${s(a.server) || 'a connected'} tool${a.tool ? ' ' + s(a.tool) : ''}`;
    default: break;
  }
  // Steps named in words already (team and subtask runs: "wrote files").
  if (/\s/.test(name)) return name.charAt(0).toUpperCase() + name.slice(1);
  if (name.startsWith('jira_')) return 'Working with Jira';
  if (name.startsWith('confluence_')) return 'Working with Confluence';
  if (name.startsWith('gitlab_')) return 'Working with GitLab';
  const named = name.replace(/_/g, ' ').trim();
  return /[a-z]/i.test(named) ? `Using ${named}` : 'Working';
}
