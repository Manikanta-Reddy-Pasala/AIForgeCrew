// Two bundles: the extension (Node, `vscode` is provided by the host) and the
// chat webview (browser). The webview shares the web UI's live-turn reducer
// (web/src/views/Chat.reduce.ts), so a turn renders the same in both.
import * as esbuild from 'esbuild';
import { readdirSync } from 'node:fs';

const watch = process.argv.includes('--watch');
const tests = process.argv.includes('--tests');

const builds = tests
  ? [{
      entryPoints: readdirSync('test').filter(f => f.endsWith('.test.ts')).map(f => `test/${f}`),
      outdir: 'out-test', platform: 'node', format: 'cjs', bundle: true,
      external: ['vscode'], sourcemap: 'inline',
    }]
  : [
      { entryPoints: ['src/extension.ts'], outfile: 'dist/extension.js', platform: 'node',
        format: 'cjs', bundle: true, external: ['vscode'], target: 'node18', sourcemap: true },
      { entryPoints: ['webview/main.ts'], outfile: 'dist/webview.js', platform: 'browser',
        format: 'iife', bundle: true, target: 'es2020', sourcemap: true },
    ];

for (const b of builds) {
  if (watch) await (await esbuild.context(b)).watch();
  else await esbuild.build(b);
}
