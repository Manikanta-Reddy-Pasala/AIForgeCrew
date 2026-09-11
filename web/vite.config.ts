import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vitejs.dev/config/
export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  // React builds a component stack from Function.name, and esbuild mangles
  // every function name in a production build — so the ErrorBoundary's
  // "in: …" block would read "at Bs / at As", which is exactly as
  // unactionable as the bare message it was added to replace. This is the
  // shipped path (Dockerfile runs `npm run build`; the API serves web/dist).
  esbuild: { keepNames: true },
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
