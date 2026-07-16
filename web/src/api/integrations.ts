import { j } from './core';

export type ConfluenceCfg = {
  base_url: string; user: string; insecure_tls: boolean;
  has_token: boolean; env_managed: boolean;
};

export type JiraCfg = ConfluenceCfg;

export type GitlabCfg = {
  base_url: string; project: string; oauth: boolean; insecure_tls: boolean;
  has_token: boolean; env_managed: boolean;
};

export type EmailCfg = {
  smtp_host: string; smtp_port: number; smtp_user: string; smtp_from: string;
  smtp_starttls: boolean;
  imap_host: string; imap_port: number; imap_user: string; imap_ssl: boolean;
  has_smtp_password: boolean; has_imap_password: boolean; env_managed: boolean;
};

export const integrationsApi = {
  getConfluence: () => j<ConfluenceCfg>('/integrations/confluence'),
  setConfluence: (cfg: { base_url?: string; token?: string; user?: string; insecure_tls?: boolean }) =>
    j<ConfluenceCfg>('/integrations/confluence', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    }),
  testConfluence: () =>
    j<{ ok: boolean; base_url?: string; auth?: string; error?: string; detail?: string; hint?: string; denied_reason?: string }>(
      '/integrations/confluence/test', { method: 'POST' }),

  getJira: () => j<JiraCfg>('/integrations/jira'),
  setJira: (cfg: { base_url?: string; token?: string; user?: string; insecure_tls?: boolean }) =>
    j<JiraCfg>('/integrations/jira', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    }),
  testJira: () =>
    j<{ ok: boolean; base_url?: string; auth?: string; user?: string; error?: string; detail?: string; hint?: string; denied_reason?: string }>(
      '/integrations/jira/test', { method: 'POST' }),

  getGitlab: () => j<GitlabCfg>('/integrations/gitlab'),
  setGitlab: (cfg: { base_url?: string; token?: string; project?: string; oauth?: boolean; insecure_tls?: boolean }) =>
    j<GitlabCfg>('/integrations/gitlab', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    }),
  testGitlab: () =>
    j<{ ok: boolean; base_url?: string; auth?: string; user?: string; error?: string; detail?: string; hint?: string }>(
      '/integrations/gitlab/test', { method: 'POST' }),

  getEmail: () => j<EmailCfg>('/integrations/email'),
  setEmail: (cfg: {
    smtp_host?: string; smtp_port?: number; smtp_user?: string; smtp_password?: string;
    smtp_from?: string; smtp_starttls?: boolean;
    imap_host?: string; imap_port?: number; imap_user?: string; imap_password?: string;
    imap_ssl?: boolean;
  }) =>
    j<EmailCfg>('/integrations/email', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    }),
  testEmail: () =>
    j<{
      ok: boolean; error?: string; hint?: string;
      smtp?: { ok: boolean; host?: string; port?: number; user?: string; error?: string } | null;
      imap?: { ok: boolean; host?: string; port?: number; user?: string; error?: string } | null;
    }>('/integrations/email/test', { method: 'POST' }),
};
