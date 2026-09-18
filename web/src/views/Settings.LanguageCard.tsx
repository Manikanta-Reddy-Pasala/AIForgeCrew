import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { chatApi, ResponseLanguage } from '../api';

type Choice = { code: string; label: string };

// The English variety and tone every model writes in for people — chat, team
// and ticket runs, Jira, Confluence, email, PR reviews. Prose only: the server
// tells the model never to change code or identifiers to match it.
export function ResponseLanguageCard() {
  const [prefs, setPrefs] = useState<ResponseLanguage | null>(null);

  useEffect(() => {
    chatApi.responseLanguage().then(setPrefs).catch(() => { /* */ });
  }, []);

  async function choose(field: 'language' | 'style', code: string) {
    if (!prefs) return;
    const prev = prefs;
    setPrefs({ ...prefs, [field]: code });          // optimistic
    try {
      const r = await chatApi.setResponseLanguage({ [field]: code });
      setPrefs(r);
      const list = field === 'language' ? r.options : r.styles;
      toast.success(`${field === 'language' ? 'Language' : 'Tone'}: `
        + (list.find(o => o.code === r[field])?.label ?? 'model default'));
    } catch (e: any) {
      setPrefs(prev);
      toast.error(e?.message || 'Failed to save');
    }
  }

  function picker(field: 'language' | 'style', label: string, list: Choice[]) {
    return (
      <label style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '4px 0' }}>
        <span style={{ width: 90, fontSize: 13 }}>{label}</span>
        <select aria-label={label} value={prefs?.[field] ?? ''}
                onChange={e => { void choose(field, e.target.value); }}
                disabled={!prefs}
                style={{ fontSize: 13, padding: '4px 8px', minWidth: 220 }}>
          {list.map(o => <option key={o.code} value={o.code}>{o.label}</option>)}
        </select>
      </label>
    );
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2 style={{ fontSize: 14 }}>Response language <span className="small muted">· everywhere the agents write</span></h2>
      <div className="subtitle" style={{ marginTop: 4, marginBottom: 12 }}>
        How the agents write for people — chat replies, Jira issues and
        comments, Confluence pages, emails, PR reviews and reports. Indian
        English uses colour, ₹, lakh/crore; US English uses color, MM/DD. Code,
        file names and commands never change. Applies from the next model call.
      </div>
      {picker('language', 'Language', prefs?.options ?? [])}
      {picker('style', 'Tone', prefs?.styles ?? [])}
    </div>
  );
}
