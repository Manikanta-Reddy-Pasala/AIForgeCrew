import { useState, useEffect } from 'react';
import { api } from '../api';
import { SearchPanel } from './Memory.SearchPanel';
import { NotesPanel } from './Memory.NotesPanel';
import { OkrPanel } from './Memory.OkrPanel';
import { OverviewPanel } from './Memory.OverviewPanel';
import { SourcesPanel } from './Memory.SourcesPanel';

// ─── Main Memory page ─────────────────────────────────────────────────────────

export default function Memory() {
  // The OKR node-DAG view is consolidated out by default (flat compacted/ briefs
  // are the memory). Only show its panel when the DAG is explicitly enabled.
  const [dagOn, setDagOn] = useState(false);
  useEffect(() => {
    api.memoryStats().then((s: any) => setDagOn(!!s?.okr_dag)).catch(() => {});
  }, []);
  return (
    <>
      <div className="page-header">
        <div>
          <h1>Memory</h1>
          <div className="subtitle">Scoped OKR briefs, indexed sources, and human-readable notes.</div>
        </div>
      </div>

      {/* Hybrid search — same engine the agents use */}
      <SearchPanel />

      {/* Markdown briefs (the memory: auto-written + compacted after chat runs) */}
      <NotesPanel />

      {/* OKR-DAG goal graph — only when explicitly enabled (AIFORGE_OKR_DAG=1) */}
      {dagOn && <OkrPanel />}

      {/* Per-datasource overview + clear */}
      <OverviewPanel />

      {/* Sources management */}
      <SourcesPanel />
    </>
  );
}
