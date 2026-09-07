/**
 * Trusting locally issued certificate authorities, from the screen.
 *
 * An estate with its own PKI hands out a root AND one or two intermediates,
 * usually as separate files, and before this the only way in was to export
 * AIFORGE_CA_BUNDLE in a unit file and restart — so anyone without shell
 * access hit "CERTIFICATE_VERIFY_FAILED: unable to get local issuer
 * certificate" against their own Jira and had nowhere to go.
 *
 * The list ADDS rather than replaces: uploading the intermediate must not
 * silently drop the root that was loaded a minute earlier. Each entry says
 * whether it is a root or an intermediate, because installing the root alone
 * and wondering why an internal host still fails is the classic mistake, and
 * a missing issuer is called out under the list.
 */
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { CaStatus } from '../api/client';

const KIND_TINT: Record<string, string> = {
  root: '#2f855a', intermediate: '#2b6cb0', 'not a CA': '#b7791f',
};

export default function CaCard() {
  const [st, setSt] = useState<CaStatus | null>(null);
  const [pem, setPem] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [msg, setMsg] = useState('');
  const files = useRef<HTMLInputElement>(null);

  const load = () => api.ca().then(setSt).catch(() => setSt(null));
  useEffect(() => { load(); }, []);

  const run = async (fn: () => Promise<CaStatus>, done = '') => {
    setBusy(true); setErr(''); setMsg('');
    try {
      setSt(await fn());
      if (done) setMsg(done);
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally { setBusy(false); }
  };

  const add = () => run(async () => {
    const next = await api.addCa(pem);
    setPem('');
    return next;
  }, 'Trusted. Retry whatever just failed — no restart needed.');

  // Several files at once: a root and its intermediates usually arrive as
  // separate .pem/.crt files, and asking for one upload per file is how the
  // second one gets forgotten.
  const pick = async (list: FileList | null) => {
    if (!list?.length) return;
    const texts = await Promise.all([...list].map(f => f.text()));
    setPem(prev => [prev.trim(), ...texts].filter(Boolean).join('\n'));
  };

  const envSet = !!st && st.source !== '' && st.source !== 'ui';

  return (
    <div className="card">
      <h3>Local certificate authorities</h3>
      <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 10 }}>
        Add your organisation&apos;s root and any intermediates to trust
        internal HTTPS everywhere — the model endpoint, Jira, Confluence,
        GitLab, and git, curl and npm. Verification stays on; this adds
        issuers, it does not skip the check.
      </div>

      {!!st?.certificates.length && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 12, marginBottom: 6 }}>
            {st.certificates.length} trusted, from{' '}
            <b>{st.source === 'ui' ? 'this screen' : st.source}</b>
            {!st.readable && (
              <span style={{ color: 'var(--danger, #b00)' }}>
                {' '}— the file cannot be read
              </span>
            )}
          </div>
          {st.certificates.map(c => (
            <div key={c.sha256} style={{
              display: 'flex', alignItems: 'center', gap: 8, fontSize: 11,
              padding: '5px 8px', marginBottom: 4,
              border: '1px solid var(--border, #ccc)', borderRadius: 6,
            }}>
              <span style={{
                fontSize: 10, padding: '1px 6px', borderRadius: 10,
                border: `1px solid ${KIND_TINT[c.kind] || '#888'}`,
                color: KIND_TINT[c.kind] || '#888', whiteSpace: 'nowrap',
              }}>{c.kind}</span>
              <span style={{ flex: 1, minWidth: 0 }}>
                <div style={{ overflow: 'hidden', textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap' }}>
                  {c.subject || '(subject unreadable)'}
                </div>
                <div style={{ opacity: 0.65 }}>
                  sha256 {c.sha256.slice(0, 24)}…
                  {c.not_after ? ` · expires ${c.not_after.slice(0, 10)}` : ''}
                </div>
              </span>
              {st.source === 'ui' && (
                <button type="button" className="ghost" disabled={busy}
                        onClick={() => run(() => api.removeCa(c.sha256))}>
                  Remove
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {st?.warnings?.map(w => (
        <div key={w} style={{ fontSize: 11, color: 'var(--warn, #b7791f)',
                              marginBottom: 6 }}>⚠ {w}</div>
      ))}

      {envSet && (
        <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 8 }}>
          An environment variable is set, and it wins over anything added here.
        </div>
      )}

      <textarea
        value={pem}
        onChange={e => setPem(e.target.value)}
        placeholder={'Paste one or more certificates\n-----BEGIN CERTIFICATE-----\n…'}
        rows={5}
        style={{ width: '100%', fontFamily: 'monospace', fontSize: 11 }}
      />
      <div className="row" style={{ gap: 8, marginTop: 8 }}>
        <button type="button" onClick={add} disabled={busy || !pem.trim()}>
          Add to trusted list
        </button>
        <button type="button" className="ghost"
                onClick={() => files.current?.click()} disabled={busy}>
          Choose .pem files
        </button>
        <input ref={files} type="file" multiple
               accept=".pem,.crt,.cer,.ca-bundle,.chain"
               style={{ display: 'none' }}
               onChange={e => pick(e.target.files)} />
        {st?.source === 'ui' && !!st.certificates.length && (
          <button type="button" className="ghost" disabled={busy}
                  onClick={() => run(() => api.clearCa())}>
            Remove all
          </button>
        )}
      </div>

      {err && <div style={{ color: 'var(--danger, #b00)', fontSize: 12,
                            marginTop: 8 }}>{err}</div>}
      {msg && <div style={{ fontSize: 12, marginTop: 8 }}>{msg}</div>}
    </div>
  );
}
