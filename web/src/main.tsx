import React, { useEffect, useState, Suspense, lazy } from 'react';
import { createRoot } from 'react-dom/client';
import {
  BrowserRouter, Routes, Route, NavLink, Navigate, useLocation, matchPath,
} from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from 'sonner';
import './styles.css';
import { Icon } from './icons';
import { api } from './api';

// Dashboard is the biggest view (pulls recharts). Lazy-load so the
// main bundle stays small and other pages load instantly.
const Home         = lazy(() => import('./views/Home'));
const Dashboard    = lazy(() => import('./views/Dashboard'));
const Tickets      = lazy(() => import('./views/Tickets'));
const TicketDetail = lazy(() => import('./views/TicketDetail'));
const Agents       = lazy(() => import('./views/Agents'));
const Logs         = lazy(() => import('./views/Logs'));
const Memory       = lazy(() => import('./views/Memory'));
const Chat         = lazy(() => import('./views/Chat'));
const Library      = lazy(() => import('./views/Library'));
const Tools        = lazy(() => import('./views/Tools'));
const Kanban       = lazy(() => import('./views/Kanban'));
const Trace        = lazy(() => import('./views/Trace'));
const LlmTrace     = lazy(() => import('./views/LlmTrace'));
const WorkflowGraph = lazy(() => import('./views/WorkflowGraph'));
const Perf          = lazy(() => import('./views/Perf'));

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
      { to: '/dashboard', label: 'Dashboard',  icon: 'Dashboard' },
      { to: '/board',     label: 'Board',      icon: 'Board' },
      { to: '/tickets',   label: 'Tickets',    icon: 'Tickets' },
      { to: '/chat',      label: 'Chat',       icon: 'Chat' },
      { to: '/',          label: 'Settings',   icon: 'Tool',    end: true },
    ],
  },
  {
    group: 'Reason',
    items: [
      { to: '/tools',     label: 'MCP Tools',  icon: 'Tool' },
      { to: '/memory',    label: 'Memory',     icon: 'Memory' },
      { to: '/skills',    label: 'Skills',     icon: 'Tool' },
      { to: '/workflows', label: 'Workflows',  icon: 'Tool' },
      { to: '/rules',     label: 'Rules',      icon: 'Tool' },
    ],
  },
  {
    group: 'Observe',
    items: [
      { to: '/agents',   label: 'Agents',     icon: 'Agents' },
      { to: '/workflow', label: 'Workflow',   icon: 'Tool' },
      { to: '/perf',     label: 'Perf',       icon: 'Tool' },
      { to: '/logs',     label: 'Live logs',  icon: 'Logs' },
    ],
  },
];

const TITLE_MAP: Record<string, string> = {
  '/':           'Home',
  '/dashboard':  'Dashboard',
  '/board':      'Board',
  '/tickets':    'Tickets',
  '/chat':       'Chat',
  '/tools':      'MCP Tools',
  '/memory':     'Memory',
  '/skills':     'Skills',
  '/workflows':  'Workflows',
  '/rules':      'Rules',
  '/agents':     'Agents',
  '/workflow':   'Workflow',
  '/perf':       'Perf',
  '/logs':       'Live logs',
};

function useTitle(pathname: string): string {
  if (matchPath('/tickets/:id', pathname)) return 'Ticket';
  if (matchPath('/logs/:role', pathname)) return 'Live logs';
  return TITLE_MAP[pathname] || 'AIForge';
}

function TopBar({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const loc = useLocation();
  const title = useTitle(loc.pathname);

  return (
    <div className="topbar">
      <button className="icon" onClick={onToggleSidebar} title="Toggle sidebar" aria-label="Toggle sidebar">
        <Icon.PanelLeft size={16} />
      </button>
      <div>
        <div className="topbar-title">{title}</div>
      </div>
      <div className="topbar-spacer" />
    </div>
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
            <Route path="/" element={<Home />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/board" element={<Kanban />} />
            <Route path="/tickets" element={<Tickets />} />
            <Route path="/tickets/:id" element={<TicketDetail />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/skills" element={<Library kind="skills" />} />
            <Route path="/workflows" element={<Library kind="workflows" />} />
            <Route path="/rules" element={<Library kind="rules" />} />
            <Route path="/tools" element={<Tools />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/logs/:role" element={<Logs />} />
            <Route path="/memory" element={<Memory />} />
            {/* Legacy Settings merged into Home/Setup; redirect old links. */}
            <Route path="/settings" element={<Navigate to="/" replace />} />
            <Route path="/trace/:id" element={<Trace />} />
            <Route path="/llm-trace/:id" element={<LlmTrace />} />
            <Route path="/workflow" element={<WorkflowGraph />} />
            <Route path="/perf"     element={<Perf />} />
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
