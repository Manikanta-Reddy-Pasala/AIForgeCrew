// Runs e2e/suite.ts inside a real VS Code (downloaded by @vscode/test-electron)
// against a live AIForge API. Needs a display: run under xvfb-run on a server.
//   AIFORGE_E2E_API=http://127.0.0.1:18799 AIFORGE_E2E_WS=/tmp/ws xvfb-run -a npm run test:e2e
import * as path from 'node:path';
import { runTests } from '@vscode/test-electron';

(async () => {
  const ws = process.env.AIFORGE_E2E_WS;
  if (!ws || !process.env.AIFORGE_E2E_API) {
    console.error('set AIFORGE_E2E_API (a running AIForge) and AIFORGE_E2E_WS (a git repo folder)');
    process.exit(2);
  }
  await runTests({
    extensionDevelopmentPath: path.resolve(__dirname, '..'),
    extensionTestsPath: path.resolve(__dirname, 'suite.js'),
    launchArgs: [ws, '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes'],
  });
})().catch(e => { console.error(e); process.exit(1); });
