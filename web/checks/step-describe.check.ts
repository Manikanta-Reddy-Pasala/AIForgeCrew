import { describeCommand, describeStep } from '../src/stepDescribe.ts';

function eq(got: string, want: string, msg: string): void {
  if (got !== want) throw new Error(`${msg}: got "${got}", want "${want}"`);
}

// Commands, read for what they are for.
eq(describeCommand('pytest -q tests/'), 'Running the tests', 'pytest');
eq(describeCommand('cd shopkit && python -m pytest -x'), 'Running the tests', 'cd then pytest');
eq(describeCommand('npm test'), 'Running the tests', 'npm test');
eq(describeCommand('./mvnw test -Dtest=Foo'), 'Running the tests', 'maven test');
eq(describeCommand('git status --short'), 'Checking which files changed', 'git status');
eq(describeCommand('git diff HEAD~1'), 'Looking at what changed in the code', 'git diff');
eq(describeCommand('git add shopkit/models.py'), 'Staging files for the next commit', 'git add');
// A commit whose message says "pytest" is still a commit.
eq(describeCommand('git commit -m "fix: make pytest pass"'), 'Saving the changes as a commit', 'git commit');
eq(describeCommand('git push origin main'), 'Sending the commits to the remote', 'git push');
eq(describeCommand('git checkout -b feature/coupons'), 'Creating branch feature/coupons', 'new branch');
eq(describeCommand('cat shopkit/models.py'), 'Reading shopkit/models.py', 'cat');
eq(describeCommand('cat /home/me/work/repo/shopkit/models.py'), 'Reading …/shopkit/models.py', 'long path');
eq(describeCommand('head -n 50 README.md'), 'Reading README.md', 'head');
eq(describeCommand("sed -n '1,80p' shopkit/orders.py"), 'Reading part of shopkit/orders.py', 'sed -n');
eq(describeCommand('grep -rn "def checkout" shopkit'), 'Searching for "def checkout"', 'grep');
eq(describeCommand('ls -la tests'), 'Listing tests', 'ls');
eq(describeCommand('ls'), 'Listing the files here', 'bare ls');
eq(describeCommand('pip install -r requirements.txt'), 'Installing Python packages', 'pip');
eq(describeCommand('npm run build'), 'Building the app', 'npm build');
eq(describeCommand('python scripts/migrate.py --dry-run'), 'Running scripts/migrate.py', 'python script');
eq(describeCommand('curl -s https://api.example.com/v1/x'), 'Calling api.example.com', 'curl');
eq(describeCommand('some-unknown-tool --flag'), 'Running a command', 'fallback');

// Tool steps.
eq(describeStep('file_read', { path: 'shopkit/models.py' }), 'Reading shopkit/models.py', 'file_read');
eq(describeStep('read_files', { paths: ['a.py', 'b.py', 'c.py', 'd.py'] }), 'Reading a.py, b.py and 2 more', 'read_files');
eq(describeStep('file_patch', { path: 'shopkit/orders.py', old_text: 'x', new_text: 'y' }), 'Editing shopkit/orders.py', 'file_patch');
eq(describeStep('multi_edit', { edits: [{ path: 'a.py' }, { path: 'a.py' }, { path: 'b.py' }] }), 'Editing a.py and b.py', 'multi_edit');
eq(describeStep('grep', { pattern: 'coupon', path: 'shopkit' }), 'Searching shopkit for "coupon"', 'grep tool');
eq(describeStep('run_command', { cmd: 'git push' }), 'Sending the commits to the remote', 'run_command');
eq(describeStep('run_tests', {}), 'Running the tests', 'run_tests');
eq(describeStep('web_fetch', { url: 'https://docs.python.org/3/' }), 'Opening docs.python.org', 'web_fetch');
eq(describeStep('wrote files', {}), 'Wrote files', 'team step named in words');
eq(describeStep('jira_dashboards', {}), 'Working with Jira', 'jira fallback');
eq(describeStep('brand_new_tool', {}), 'Using brand new tool', 'unknown tool');
eq(describeStep('file_read', undefined), 'Reading a file', 'no args');

// Cases that used to read wrong.
eq(describeCommand('pip install pytest'), 'Installing Python packages', 'installing a test tool is not running tests');
eq(describeCommand('npm i -D vitest'), 'Installing packages', 'npm i');
eq(describeCommand('cat pytest.ini'), 'Reading pytest.ini', 'reading a test config');
eq(describeCommand('grep -rn pytest .'), 'Searching for "pytest"', 'searching for a test tool');
eq(describeCommand('which pytest'), 'Checking a tool is installed', 'which');
eq(describeCommand('mvn test-compile'), 'Running mvn test-compile', 'mvn test-compile is not a test run');
eq(describeCommand("cat > app.py <<'EOF'"), 'Writing app.py', 'heredoc write');
eq(describeCommand('cat <<EOF > app.py'), 'Writing app.py', 'heredoc write, other order');
eq(describeCommand('echo hi >> notes.txt'), 'Writing notes.txt', 'append');
eq(describeCommand('pytest -q 2>&1 > /dev/null'), 'Running the tests', 'redirect to nowhere');
eq(describeCommand('bash -lc "pytest -q"'), 'Running the tests', 'bash -lc');
eq(describeCommand('bash -lc "ls"'), 'Listing the files here', 'bash -lc ls');
eq(describeCommand('node -e "console.log(1)"'), 'Running a short script', 'node -e');
eq(describeCommand('python -u train.py'), 'Running train.py', 'python flag');
eq(describeCommand('grep -E "foo|bar" src'), 'Searching for "foo|bar"', 'pipe inside quotes');
eq(describeCommand('grep -A 3 foo f.py'), 'Searching for "foo"', 'flag value skipped');
eq(describeCommand('rg -t py foo'), 'Searching for "foo"', 'rg type flag');
eq(describeCommand('uv run python scripts/sync.py'), 'Running scripts/sync.py', 'uv run');
eq(describeCommand('uv run pytest'), 'Running the tests', 'uv run pytest');
eq(describeCommand('npx jest --watch'), 'Running the tests', 'npx jest');
eq(describeCommand('python3 -m pip install requests'), 'Installing Python packages', 'python -m pip');
eq(describeCommand('git checkout -b feat origin/main'), 'Creating branch feat', 'branch from a base');
eq(describeCommand('git checkout -- a.py'), 'Throwing away changes to files', 'checkout --');
eq(describeCommand('git checkout .'), 'Throwing away changes to files', 'checkout .');
eq(describeCommand('git stash pop'), 'Bringing back set-aside changes', 'stash pop');
eq(describeCommand('git branch -D old'), 'Deleting branch old', 'branch delete');
eq(describeCommand('git -C repo status'), 'Checking which files changed', 'git -C');
eq(describeCommand('git --no-pager diff'), 'Looking at what changed in the code', 'git --no-pager');
eq(describeCommand('FOO=1 git commit -m x'), 'Saving the changes as a commit', 'env prefix');
eq(describeCommand('cd "my dir" && pytest'), 'Running the tests', 'cd into a quoted folder');
eq(describeCommand('cd x && git commit -m "y"'), 'Saving the changes as a commit', 'cd then commit');
eq(describeStep('read_lines', { path: 'a.py', start: 1, end: 0 }), 'Reading lines 1–end of a.py', 'read_lines to the end');
eq(describeStep('run_shell', { cmd: 'git status' }), 'Checking which files changed', 'other shell tool');
eq(describeStep('memory_lookup', {}), 'Searching memory', 'no query');
eq(describeStep('codegraph_query', {}), 'Looking at the code map', 'no symbol');
eq(describeStep('rename_symbol', {}), 'Renaming a name in the code', 'no names');
eq(describeStep('committed', {}), 'Saved the changes as a commit', 'team commit step');
eq(describeStep('?', {}), 'Working', 'unnamed step');
eq(describeStep('list_dir', { path: '/' }), 'Listing /', 'root folder');
eq(describeStep('file_read', { path: 'C:\\work\\repo\\shopkit\\models.py' }), 'Reading …/shopkit/models.py', 'windows path');
eq(describeCommand('x'.repeat(100000) + ' && pytest'), 'Running a command', 'huge command stays quick');

console.log('step-describe ok');
