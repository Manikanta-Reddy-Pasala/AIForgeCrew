// Barrel — re-exports the whole API client, split by domain into sibling
// modules. Every name previously exported from `src/api.ts` remains importable
// from this same `../api` path. Behaviour is identical; this is a mechanical
// split only.
export * from './core';
export * from './agents';
export * from './memory';
export * from './jobs';
export * from './workflows';
export * from './client';
export * from './chat';
export * from './mcp';
export * from './integrations';
export * from './rules';
