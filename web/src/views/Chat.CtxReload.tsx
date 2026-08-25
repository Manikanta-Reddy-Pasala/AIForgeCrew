import { useState } from 'react';
import { toast } from 'sonner';
import { chatApi } from '../api';

// Context-window (re)load control: type a context size in K and reload the
// given model on the LM Studio host at that window. No preset sizes baked in
// — the operator types any value; the backend enforces its own floor/ceiling.
export function CtxReload({ model, onLoaded }: { model: string; onLoaded?: () => void }) {
  const [k, setK] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const kn = Number(k);
  const valid = !!model && Number.isFinite(kn) && kn > 0;
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, marginLeft: 4 }}>
      <input
        type="number"
        className="chat-model-select"
        style={{ width: 70 }}
        value={k}
        min={1}
        placeholder="ctx K"
        disabled={loading || !model}
        title="Context window in K tokens to (re)load this model at"
        onChange={e => setK(e.target.value)}
      />
      <button type="button"
        className="ghost sm"
        disabled={loading || !valid}
        title={model ? `Reload ${model} at ${k || '?'}K context (takes a few seconds)` : 'Pick a model first'}
        onClick={async () => {
          setLoading(true);
          try {
            const res = await chatApi.reloadModel(model, Math.round(kn * 1024));
            toast.success(`Loaded at ${Math.round(res.context_length / 1024)}K context`);
            onLoaded?.();
          } catch (err: any) {
            toast.error(`Reload failed: ${err.message}`);
          } finally {
            setLoading(false);
          }
        }}
      >
        {loading ? 'Loading…' : 'Reload @ ctx'}
      </button>
    </span>
  );
}
