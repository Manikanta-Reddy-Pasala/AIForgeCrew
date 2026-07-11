/* Tiny inline SVG icon set. Feather-style, 1.5px stroke, 18×18. */
import React from 'react';

type P = React.SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 18, children, ...rest }: P & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const Icon = {
  Dashboard: (p: P) => <Svg {...p}><rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" /></Svg>,
  Board: (p: P) => <Svg {...p}><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M9 3v18M15 3v18" /></Svg>,
  Tickets: (p: P) => <Svg {...p}><path d="M20 12V7a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v5a2 2 0 1 1 0 4v1a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-1a2 2 0 1 1 0-4Z" /><path d="M12 5v14" strokeDasharray="1 2" /></Svg>,
  Chat: (p: P) => <Svg {...p}><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z" /></Svg>,
  Tool: (p: P) => <Svg {...p}><path d="M14.7 6.3a4 4 0 0 0 5 5l-1.4 1.4a4 4 0 0 1-5.6 0L5.6 19.8a2 2 0 0 1-2.8-2.8l7.1-7.1a4 4 0 0 1 0-5.6Z" /></Svg>,
  Agents: (p: P) => <Svg {...p}><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4M9 10h6" /><circle cx="9" cy="9" r="1" /><circle cx="15" cy="9" r="1" /></Svg>,
  Logs: (p: P) => <Svg {...p}><path d="M4 6h16M4 12h16M4 18h10" /></Svg>,
  Memory: (p: P) => <Svg {...p}><rect x="4" y="4" width="16" height="16" rx="2" /><path d="M9 4v16M15 4v16M4 9h5M4 15h5M15 9h5M15 15h5" /></Svg>,
  Search: (p: P) => <Svg {...p}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></Svg>,
  ChevronRight: (p: P) => <Svg {...p}><path d="m9 6 6 6-6 6" /></Svg>,
  ChevronLeft: (p: P) => <Svg {...p}><path d="m15 6-6 6 6 6" /></Svg>,
  Plus: (p: P) => <Svg {...p}><path d="M12 5v14M5 12h14" /></Svg>,
  Check: (p: P) => <Svg {...p}><path d="m5 12 5 5L20 7" /></Svg>,
  X: (p: P) => <Svg {...p}><path d="M6 6l12 12M18 6 6 18" /></Svg>,
  Refresh: (p: P) => <Svg {...p}><path d="M21 12a9 9 0 1 1-3.1-6.8" /><path d="M21 4v6h-6" /></Svg>,
  Send: (p: P) => <Svg {...p}><path d="M22 2 11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7Z" /></Svg>,
  Sparkles: (p: P) => <Svg {...p}><path d="M12 3v4M12 17v4M5 12H1M23 12h-4M6.2 6.2l2.8 2.8M15 15l2.8 2.8M6.2 17.8 9 15M15 9l2.8-2.8" /></Svg>,
  Drag: (p: P) => <Svg {...p}><circle cx="9" cy="5" r="1" fill="currentColor" /><circle cx="9" cy="12" r="1" fill="currentColor" /><circle cx="9" cy="19" r="1" fill="currentColor" /><circle cx="15" cy="5" r="1" fill="currentColor" /><circle cx="15" cy="12" r="1" fill="currentColor" /><circle cx="15" cy="19" r="1" fill="currentColor" /></Svg>,
  Layers: (p: P) => <Svg {...p}><path d="m12 2 10 6-10 6L2 8l10-6Z" /><path d="m2 16 10 6 10-6" /><path d="m2 12 10 6 10-6" /></Svg>,
  Info: (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M12 8v.01M12 11v5" /></Svg>,
  GitBranch: (p: P) => <Svg {...p}><circle cx="6" cy="6" r="2.5" /><circle cx="6" cy="18" r="2.5" /><circle cx="18" cy="8" r="2.5" /><path d="M6 9v6M18 10c0 4-5 4-5 7" /></Svg>,
  Filter: (p: P) => <Svg {...p}><path d="M22 3H2l8 10v7l4 2v-9l8-10Z" /></Svg>,
  PanelLeft: (p: P) => <Svg {...p}><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M9 3v18" /></Svg>,
  Trash: (p: P) => <Svg {...p}><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></Svg>,
  Edit: (p: P) => <Svg {...p}><path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z" /></Svg>,
  Sun: (p: P) => <Svg {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></Svg>,
  Moon: (p: P) => <Svg {...p}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" /></Svg>,
  Folder: (p: P) => <Svg {...p}><path d="M3 7a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.4.6L11.8 7H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" /></Svg>,
  File: (p: P) => <Svg {...p}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" /><path d="M14 3v5h5" /></Svg>,
  Paperclip: (p: P) => <Svg {...p}><path d="M21.4 11.05 12.25 20.2a5.5 5.5 0 0 1-7.78-7.78l8.49-8.49a3.5 3.5 0 0 1 4.95 4.95l-8.49 8.49a1.5 1.5 0 0 1-2.12-2.12l7.78-7.78" /></Svg>,
  // Distinct nav icons (MCP / skills / workflows / rules / orchestration / perf / settings)
  Mcp: (p: P) => <Svg {...p}><path d="M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1 7 17M17 7l2.1-2.1" /><circle cx="12" cy="12" r="3.5" /></Svg>,
  Skill: (p: P) => <Svg {...p}><path d="M22 10 12 5 2 10l10 5 10-5Z" /><path d="M6 12v5c0 1 2.7 2.5 6 2.5s6-1.5 6-2.5v-5" /></Svg>,
  Workflow: (p: P) => <Svg {...p}><rect x="3" y="3" width="6" height="6" rx="1.5" /><rect x="15" y="15" width="6" height="6" rx="1.5" /><path d="M9 6h4a2 2 0 0 1 2 2v7" /></Svg>,
  Rules: (p: P) => <Svg {...p}><path d="M12 3 4 6v5c0 5 3.5 8 8 10 4.5-2 8-5 8-10V6l-8-3Z" /><path d="m9 12 2 2 4-4" /></Svg>,
  Orchestration: (p: P) => <Svg {...p}><path d="M3 12h4l2-6 4 14 2-8h6" /></Svg>,
  Gauge: (p: P) => <Svg {...p}><path d="M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z" /><path d="M13.4 10.6 19 5M3.3 17a9 9 0 1 1 17.4 0" /></Svg>,
  Settings: (p: P) => <Svg {...p}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1.08 1.65 1.65 0 0 0-.33-1.82l-.06-.06A2 2 0 1 1 7.04 3.3l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V2a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H22a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" /></Svg>,
};
