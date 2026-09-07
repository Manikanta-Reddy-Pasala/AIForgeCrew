/**
 * Trusting a locally issued certificate authority, from the screen.
 *
 * An estate with its own CA used to have exactly one way in — export
 * AIFORGE_CA_BUNDLE in a unit file and restart — so anyone without shell
 * access hit "CERTIFICATE_VERIFY_FAILED: unable to get local issuer
 * certificate" against their own Jira and had nowhere to go. Pasting the
 * certificate here trusts it everywhere at once: the model endpoint, Jira,
 * Confluence, GitLab, our own HTTP, and git, curl and npm through the
 * environment we hand to subprocesses.
 *
 * The parsed subject and fingerprint are shown back because a pasted PEM is
 * unreadable to a human: the screen has to prove the right file landed.
 */
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { CaStatus } from '../api/client';

export default function CaCard() {
  const [st, setSt] = useState<CaStatus | null>(null);
  const [pem, setPem] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [msg, setMsg] = useState('');
  const file = useRef<HTMLInputElement>(null);

  const load = () => api.ca().then(setSt).catch(() => setSt(null));
  useEffect(() => { load(); }, []);

  const save = async () => {
    setBusy(true); setErr(''); setMsg('');
    try {
      const next = await api.saveCa(pem);
      setSt(next); setPem('');
      setMsg('Trusted. Retry whatever just failed — no restart needed.');
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally { setBusy(false); }
  };

  const remove = async () => {
    setBusy(true); setErr(''); setMsg('');
    try { setSt(await api.clearCa()); } catch (e: any) {
      setErr(String(e?.message || e));
    } finally { setBusy(false); }
  };

  const pick = async (f: File | undefined) => {
    if (!f) return;
    setPem(await f.text());
  };

  const envSet = !!st && st.source !== '' && st.source !== 'ui';

  return (
    <div className="card">
      <h3>Local certificate authority</h3>
      <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 10 }}>
        Paste your organisation&apos;s root certificate to trust internal
        HTTPS everywhere — the model endpoint, Jira, Confluence, GitLab, and
        git, curl and npm. Verification stays on; this adds an issuer, it does
        not skip the check.
      </div>

      {st?.configured && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 12, marginBottom: 4 }}>
            In force from <b>{st.source === 'ui' ? 'this screen'
                                                 : st.source}</b>
            {!st.readable && (
              <span style={{ color: 'var(--danger, #b00)' }}>
                {' '}— the file cannot be read
              </span>
            )}
          </div>
          {st.certificates.map(c => (
            <div key={c.sha256} style={{
              fontSize: 11, opacity: 0.8, padding: '4px 8px', marginBottom: 4,
              border: '1px solid var(--border, #ccc)', borderRadius: 6,
            }}>
              <div>{c.subject || '(subject unreadable)'}</div>
              <div style={{ opacity: 0.7 }}>
                sha256 {c.sha256.slice(0, 32)}…
                {c.not_after ? ` · expires ${c.not_after.slice(0, 10)}` : ''}
              </div>
            </div>
          ))}
        </div>
      )}

      {envSet && (
        <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 8 }}>
          An environment variable is set, and it wins over anything saved here.
        </div>
      )}

      <textarea
        value={pem}
        onChange={e => setPem(e.target.value)}
        placeholder={'-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----'}
        rows={5}
        style={{ width: '100%', fontFamily: 'monospace', fontSize: 11 }}
      />
      <div className="row" style={{ gap: 8, marginTop: 8 }}>
        <button type="button" onClick={save} disabled={busy || !pem.trim()}>
          Trust this certificate
        </button>
        <button type="button" className="ghost"
                onClick={() => file.current?.click()} disabled={busy}>
          Choose a .pem file
        </button>
        <input ref={file} type="file" accept=".pem,.crt,.cer,.ca-bundle"
               style={{ display: 'none' }}
               onChange={e => pick(e.target.files?.[0])} />
        {st?.source === 'ui' && (
          <button type="button" className="ghost" onClick={remove}
                  disabled={busy}>
            Remove
          </button>
        )}
      </div>

      {err && <div style={{ color: 'var(--danger, #b00)', fontSize: 12,
                            marginTop: 8 }}>{err}</div>}
      {msg && <div style={{ fontSize: 12, marginTop: 8 }}>{msg}</div>}
    </div>
  );
}
