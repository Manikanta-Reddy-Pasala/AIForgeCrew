"""The cheap "is the model server there at all?" probe of llm/model_wait."""
from __future__ import annotations


def probe(url: str, api_key: str = "", timeout_s: float = 5.0) -> bool:
    """Does the endpoint answer at all? Any HTTP answer below 500 (other than
    429) counts: the server is there, the retried call will say the rest."""
    import urllib.error
    import urllib.request
    base = str(url or "").rstrip("/")
    if not base:
        return False
    req = urllib.request.Request(
        f"{base}/models", headers={"Authorization": f"Bearer {api_key}",
                                   "Accept": "application/json"})
    ctx = None
    if base.lower().startswith("https://"):
        try:
            from aiforge_core.llm._ssl import context_for
            ctx = context_for(base)
        except Exception:  # noqa: BLE001
            ctx = None
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            resp.read(1 << 16)
            return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500 and exc.code != 429
    except Exception:  # noqa: BLE001 — refused, DNS, timeout, TLS: still down
        return False


__all__ = ["probe"]
