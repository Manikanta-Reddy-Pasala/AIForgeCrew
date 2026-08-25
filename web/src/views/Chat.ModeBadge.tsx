// Per-session/turn mode chip — so 'plan' / 'team' runs are visually distinct
// from an ordinary simple chat in the sidebar (simple = no chip, it's the norm).
const MODE_CHIP: Record<string, { label: string; bg: string }> = {
  plan: { label: 'Plan', bg: '#a371f7' },
  team: { label: 'Pipeline', bg: '#2f81f7' },
};

export function ModeBadge({ mode }: Readonly<{ mode?: string }>) {
  const c = mode ? MODE_CHIP[mode] : undefined;
  if (!c) return null;   // simple (or unknown) → no chip
  return (
    <span
      title={mode === 'team'
        ? 'Ran in Team / Pipeline mode (full planner→doer→learner)'
        : 'Ran in Plan mode (read-only, proposes a plan first)'}
      style={{
        marginLeft: 6, padding: '0 6px', borderRadius: 8, fontSize: 10,
        fontWeight: 600, color: '#fff', background: c.bg, verticalAlign: 'middle',
        whiteSpace: 'nowrap',
      }}
    >{c.label}</span>
  );
}
