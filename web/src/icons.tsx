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
};
