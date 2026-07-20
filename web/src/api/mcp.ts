import { j, apiFetch } from './core';

export type McpCatalogEntry = {
  id: string; name: string; description: string; transport: string;
  url: string; homepage?: string; needs_api_key?: boolean; category?: string;
  installable: boolean;
};
export type McpServer = {
  id: string; name: string; url: string; transport: string; enabled: boolean;
  catalog_id: string; description: string; api_key_set: boolean;
};

export const mcpApi = {
  catalog: () => j<{ catalog: McpCatalogEntry[] }>('/mcp/catalog'),
  servers: () => j<{ servers: McpServer[] }>('/mcp/servers'),
  install: (body: { catalog_id: string; url?: string; name?: string; api_key?: string }) =>
    j<McpServer>('/mcp/servers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  update: (id: string, body: Partial<{ name: string; url: string; description: string; enabled: boolean; api_key: string }>) =>
    j<McpServer>(`/mcp/servers/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  remove: (id: string) =>
    apiFetch(`/mcp/servers/${id}`, { method: 'DELETE' }).then(r => {
      if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
    }),
  test: (id: string) =>
    j<{ ok: boolean; endpoint?: string; tools?: { name: string; description: string }[]; error?: string }>(
      `/mcp/servers/${id}/test`, { method: 'POST' }),
};
