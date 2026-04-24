import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Routes, Route, Link, NavLink } from 'react-router-dom';
import './styles.css';
import Dashboard from './views/Dashboard';
import Tickets from './views/Tickets';
import TicketDetail from './views/TicketDetail';
import Agents from './views/Agents';
import Logs from './views/Logs';
import Memory from './views/Memory';
import Chat from './views/Chat';
import Tools from './views/Tools';
import Kanban from './views/Kanban';

function Shell() {
  return (
    <div className="shell">
      <header className="topnav">
        <Link to="/" className="brand">AIForge</Link>
        <nav>
          <NavLink to="/" end>Dashboard</NavLink>
          <NavLink to="/board">Board</NavLink>
          <NavLink to="/tickets">Tickets</NavLink>
          <NavLink to="/chat">Chat</NavLink>
          <NavLink to="/tools">MCP Tools</NavLink>
          <NavLink to="/agents">Agents</NavLink>
          <NavLink to="/logs">Logs</NavLink>
          <NavLink to="/memory">Memory</NavLink>
        </nav>
      </header>
      <main>
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
        </Routes>
      </main>
    </div>
  );
}

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <React.StrictMode>
      <BrowserRouter basename="/ui">
        <Shell />
      </BrowserRouter>
    </React.StrictMode>,
  );
}
