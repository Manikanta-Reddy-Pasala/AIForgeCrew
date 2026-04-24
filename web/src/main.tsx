import React, { useEffect, useState, Suspense, lazy } from 'react';
import { createRoot } from 'react-dom/client';
import {
  BrowserRouter, Routes, Route, NavLink, useLocation, matchPath,
} from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from 'sonner';
import './styles.css';
import { Icon } from './icons';
import { api } from './api';

// Dashboard is the biggest view (pulls recharts). Lazy-load so the
// main bundle stays small and other pages load instantly.
const Dashboard    = lazy(() => import('./views/Dashboard'));
const Tickets      = lazy(() => import('./views/Tickets'));
const TicketDetail = lazy(() => import('./views/TicketDetail'));
const Agents       = lazy(() => import('./views/Agents'));
const Logs         = lazy(() => import('./views/Logs'));
const Memory       = lazy(() => import('./views/Memory'));
const Chat         = lazy(() => import('./views/Chat'));
const Tools        = lazy(() => import('./views/Tools'));
const Kanban       = lazy(() => import('./views/Kanban'));
const Settings     = lazy(() => import('./views/Settings'));
const Trace        = lazy(() => import('./views/Trace'));

function RouteFallback() {
  return (
    <div style={{ padding: 24 }}>
      <div className="skeleton" style={{ width: 200, height: 24, marginBottom: 12 }} />
      <div className="skeleton" style={{ width: '100%', height: 120 }} />
    </div>
  );
}

const qc = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

type NavItem = {
  to: string;
  label: string;
  icon: keyof typeof Icon;
  end?: boolean;
};

const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: 'Operate',
    items: [
      { to: '/',        label: 'Dashboard',  icon: 'Dashboard', end: true },
      { to: '/board',   label: 'Board',      icon: 'Board' },
      { to: '/tickets', label: 'Tickets',    icon: 'Tickets' },
    ],
  },
  {
    group: 'Reason',
    items: [
      { to: '/chat',    label: 'Chat',       icon: 'Chat' },
      { to: '/tools',   label: 'MCP Tools',  icon: 'Tool' },
      { to: '/memory',  label: 'Memory',     icon: 'Memory' },
    ],
  },
  {
    group: 'Observe',
    items: [
      { to: '/agents',  label: 'Agents',     icon: 'Agents' },
      { to: '/logs',    label: 'Live logs',  icon: 'Logs' },
    ],
  },
  {
    group: 'Configure',
    items: [
      { to: '/settings', label: 'Settings', icon: 'Tool' },
    ],
  },
];

const TITLE_MAP: Record<string, string> = {
  '/':        'Dashboard',
  '/board':   'Board',
  '/tickets': 'Tickets',
  '/chat':    'Chat',
  '/tools':   'MCP Tools',
  '/memory':  'Memory',
  '/agents':  'Agents',
  '/logs':    'Live logs',
};

function useTitle(pathname: string): string {
  if (matchPath('/tickets/:id', pathname)) return 'Ticket';
  if (matchPath('/logs/:role', pathname)) return 'Live logs';
  return TITLE_MAP[pathname] || 'AIForge';
}

function TopBar({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const loc = useLocation();
  const title = useTitle(loc.pathname);
  const [health, setHealth] = useState<any>(null);
  useEffect(() => {
    let alive = true;
    const tick = () => api.health().then(h => alive && setHealth(h)).catch(() => alive && setHealth({ ok: false }));
    tick();
    const id = setInterval(tick, 15_000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  return (
    <div className="topbar">
      <button className="icon" onClick={onToggleSidebar} title="Toggle sidebar" aria-label="Toggle sidebar">
        <Icon.PanelLeft size={16} />
      </button>
      <div>
        <div className="topbar-title">{title}</div>
      </div>
      <div className="topbar-spacer" />
      <div className="topbar-health">
        <HealthPill label="Postgres"  on={!!health?.postgres} />
        <HealthPill label="LM Studio" on={!!health?.lm_studio} />
      </div>
    </div>
  );
}

function HealthPill({ label, on }: { label: string; on: boolean }) {
  return (
    <span className={`health-dot ${on ? '' : 'down'}`}>
      <span className="dot" />
      {label}
    </span>
  );
}

function Sidebar() {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">AF</div>
        <div className="brand-text">AIForge</div>
      </div>
      {NAV.map(g => (
        <div key={g.group} className="nav-group">
          <div className="nav-group-label">{g.group}</div>
          {g.items.map(it => {
            const I = Icon[it.icon];
            return (
              <NavLink
                key={it.to}
                to={it.to}
                end={it.end}
                className={({ isActive }) =>
                  'nav-link' + (isActive ? ' active' : '')
                }
              >
                <span className="nav-icon"><I size={16} /></span>
                <span className="nav-link-label">{it.label}</span>
              </NavLink>
            );
          })}
        </div>
      ))}
    </aside>
  );
}

function Shell() {
  const [collapsed, setCollapsed] = useState(() =>
    window.matchMedia('(max-width: 900px)').matches,
  );
  return (
    <div className={`shell${collapsed ? ' collapsed' : ''}`}>
      <Sidebar />
      <TopBar onToggleSidebar={() => setCollapsed(c => !c)} />
      <main className="page">
        <Suspense fallback={<RouteFallback />}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/board" element={<Kanban />} />
            <Route path="/tickets" element={<Tickets />} />
            <Route path="/tickets/:id" element={<TicketDetail />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/tools" element={<Tools />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/logs/:role" element={<Logs />} />
            <Route path="/memory" element={<Memory />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/trace/:id" element={<Trace />} />
          </Routes>
        </Suspense>
      </main>
    </div>
  );
}

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <React.StrictMode>
      <QueryClientProvider client={qc}>
        <BrowserRouter basename="/ui">
          <Shell />
        </BrowserRouter>
        <Toaster
          theme="dark"
          position="bottom-right"
          richColors
          closeButton
          toastOptions={{
            style: {
              background: 'var(--bg-3)',
              border: '1px solid var(--border-1)',
              color: 'var(--fg-0)',
            },
          }}
        />
      </QueryClientProvider>
    </React.StrictMode>,
  );
}
