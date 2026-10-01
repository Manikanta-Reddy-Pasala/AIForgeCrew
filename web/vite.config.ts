import fs from 'node:fs';
import path from 'node:path';
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

// The speech runtime's own loader asks jsDelivr for a dev build of
// onnxruntime-web. That host is closed from a lot of networks (the console
// shows ERR_CONNECTION_CLOSED). These two files already sit in node_modules;
// serve them from this app instead.
const SPEECH_RUNTIME_FILES = [
  'ort-wasm-simd-threaded.asyncify.wasm',
  'ort-wasm-simd-threaded.asyncify.mjs',
];

function speechRuntime(): Plugin {
  const srcDir = path.resolve('node_modules/onnxruntime-web/dist');
  const typeFor = (name: string) => (
    name.endsWith('.wasm') ? 'application/wasm' : 'text/javascript'
  );
  return {
    name: 'speech-runtime',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = req.url?.split('?')[0] ?? '';
        const name = SPEECH_RUNTIME_FILES.find(file => url.endsWith(`/speech/${file}`));
        if (!name) {
          next();
          return;
        }
        const file = path.join(srcDir, name);
        res.setHeader('Content-Type', typeFor(name));
        res.setHeader('Content-Length', fs.statSync(file).size);
        fs.createReadStream(file).pipe(res);
      });
    },
    generateBundle() {
      for (const name of SPEECH_RUNTIME_FILES) {
        this.emitFile({
          type: 'asset',
          fileName: `speech/${name}`,
          source: fs.readFileSync(path.join(srcDir, name)),
        });
      }
    },
  };
}

// https://vitejs.dev/config/
export default defineConfig({
  base: '/ui/',
  plugins: [react(), speechRuntime()],
  // React builds a component stack from Function.name, and the minifier
  // mangles every function name in a production build — so the
  // ErrorBoundary's "in: …" block would read "at Bs / at As", which is exactly
  // as unactionable as the bare message it was added to replace. Vite 8
  // bundles and minifies with rolldown/oxc, so the old `esbuild.keepNames`
  // was silently ignored; rolldown's own output.keepNames is the switch.
  // The speech model is imported only when the mic is clicked. Pre-bundling
  // it breaks its WASM URL resolution.
  optimizeDeps: {
    exclude: ['@huggingface/transformers'],
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8799',
        changeOrigin: true,
      },
    },
  },
  build: {
    // Keep a single bundle simple, but split react-query out so the initial
    // JS parse cost is lower on pages that don't need it.
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        keepNames: true,
        // Vite 8 bundles with rolldown, which replaced the `manualChunks`
        // object form with `advancedChunks.groups` (it only accepts
        // manualChunks as a FUNCTION, so the old object silently became
        // "manualChunks is not a function" at build time).
        advancedChunks: {
          groups: [
            { name: 'query', test: /[\\/]node_modules[\\/]@tanstack[\\/]react-query[\\/]/ },
          ],
        },
      },
    },
  },
});
