/**
 * Chat failover: the models a chat turn switches to when its own model stops
 * answering (no first token / no new tokens past the threshold, or several
 * failed sends in a row). The turn then stays on that model — it does not
 * flap back. Up to three models, in order, picked from the registry above.
 * Nothing listed = no failover. AIFORGE_LLM_FAILOVER=0 turns it off.
 */
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { api } from '../api/client';
import type { ChatFailover } from '../api/client';

export default function FailoverCard() {
  const [st, setSt] = useState<ChatFailover | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [stall, setStall] = useState('');
  const [fails, setFails] = useState('');
  const [busy, setBusy] = useState(false);

  function show(v: ChatFailover) {
    setSt(v);
    setModels(v.models);
    setStall(v.stall_s_saved == null ? '' : String(v.stall_s_saved));
    setFails(v.fails_saved == null ? '' : String(v.fails_saved));
  }

  useEffect(() => {
    api.chatFailover().then(show).catch(() => { /* older build: card stays empty */ });
  }, []);

  if (!st) return null;

  const num = (raw: string) => (raw.trim() === '' ? null : Number(raw));

  async function save() {
    const s = num(stall), f = num(fails);
    if ((s != null && !Number.isInteger(s)) || (f != null && !Number.isInteger(f))) {
      toast.error('Thresholds must be whole numbers (or empty for the default)');
      return;
    }
    setBusy(true);
    try {
      show(await api.setChatFailover(models.filter(Boolean), s, f));
      toast.success('Failover saved');
    } catch (e: any) {
      toast.error(`Save failed: ${e?.message || 'unknown'}`);
    } finally {
      setBusy(false);
    }
  }

  function setSlot(i: number, id: string) {
    setModels(prev => {
      const next = [...prev];
      next[i] = id;
      return next.filter((m, j) => m && next.indexOf(m) === j);
    });
  }

  const slots = Math.min(st.max_models, models.length + 1);
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2 style={{ fontSize: 14 }}>Chat failover</h2>
      <div className="subtitle" style={{ marginTop: 6, marginBottom: 10 }}>
        When the chat&apos;s model stops answering — no first token, or no new
        tokens, for the stall time below, or several failed sends in a row — the
        same step is sent to the next model in this list and the turn stays on it.
        An answer that is still streaming never fails over. Leave the list empty
        for no failover.
        {!st.enabled && <strong> Off: AIFORGE_LLM_FAILOVER=0 is set.</strong>}
      </div>
      <div className="row" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        {Array.from({ length: slots }, (_, i) => (
          <label key={i} className="small" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span>{i + 1}.</span>
            <select value={models[i] || ''} disabled={busy}
                    onChange={e => setSlot(i, e.target.value)} aria-label={`Failover model ${i + 1}`}>
              <option value="">— none —</option>
              {st.options.map(o => (
                <option key={o.id} value={o.id}
                        disabled={models.includes(o.id) && models[i] !== o.id}>
                  {o.label || o.model}{o.base_url ? ` (${o.base_url})` : ''}
                </option>
              ))}
            </select>
          </label>
        ))}
        <label className="small" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span>Stall time (s)</span>
          <input type="number" min={30} max={3600} step={1} value={stall}
                 placeholder={String(st.stall_s)} disabled={busy}
                 onChange={e => setStall(e.target.value)} style={{ width: 110 }}
                 aria-label="Failover stall time" />
        </label>
        <label className="small" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span>Failed sends</span>
          <input type="number" min={1} max={20} step={1} value={fails}
                 placeholder={String(st.fails)} disabled={busy}
                 onChange={e => setFails(e.target.value)} style={{ width: 90 }}
                 aria-label="Failover failed sends" />
        </label>
        <button type="button" onClick={save} disabled={busy}>Save</button>
      </div>
    </div>
  );
}
