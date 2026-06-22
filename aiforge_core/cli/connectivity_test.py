"""One-shot connectivity + TLS test for the configured model endpoint.

Run via ``./run.sh --test`` or ``python -m aiforge_core.cli.connectivity_test``.

Resolves the model ``base_url`` exactly the way the provider does (env
overrides → stored agent_config row → default), then GETs ``{base}/v1/models``
through :func:`aiforge_core.llm.providers.openai_compatible.probe`, which
applies the *current* SSL settings (``AIFORGE_LLM_SSL_VERIFY`` /
``AIFORGE_LLM_CA_BUNDLE``, host-scoped — see ``aiforge_core.net.ssl``).

Prints the effective base_url and whether TLS verify is on/off (never the
api key), then OK / FAIL with the error. Exits non-zero on failure so it
is usable in scripts and CI smoke checks.
"""
from __future__ import annotations

import os
import sys

_FALSEY = {"0", "false", "no", "off", ""}
_DEFAULT_BASE = "http://127.0.0.1:1234/v1"


def _verify_on() -> bool:
    raw = os.environ.get("AIFORGE_LLM_SSL_VERIFY")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def _resolve_endpoint(role: str = "doer") -> tuple[str, str | None]:
    """Return ``(base_url, api_key)`` for the model endpoint under test.

    Prefers the live agent_config resolution so the printed base_url
    matches what the running app would actually call; falls back to the
    documented env precedence if the config layer is unavailable.
    """
    try:
        from aiforge_core.config import agent_config as _acfg
        cfg = _acfg.resolve_litellm(role)
        base = cfg.get("base_url") or cfg.get("api_base")
        if base:
            return str(base), cfg.get("api_key")
    except Exception:  # noqa: BLE001 — fall back to env-only resolution.
        pass

    role_up = role.upper()
    base = (
        os.environ.get(f"AIFORGE_{role_up}_BASE_URL")
        or os.environ.get("AIFORGE_OPENAI_COMPAT_BASE_URL")
        or os.environ.get("AIFORGE_LM_BASE_URL")
        or _DEFAULT_BASE
    )
    api_key = (
        os.environ.get("AIFORGE_OPENAI_COMPAT_API_KEY")
        or os.environ.get(f"AIFORGE_{role_up}_API_KEY")
        or os.environ.get("AIFORGE_LM_API_KEY")
    )
    return base, api_key


def main(argv: list[str] | None = None) -> int:
    role = (argv or sys.argv[1:] or ["doer"])[0]
    base_url, api_key = _resolve_endpoint(role)

    ca = os.environ.get("AIFORGE_LLM_CA_BUNDLE", "").strip()
    is_https = str(base_url).lower().startswith("https://")
    if ca:
        tls = f"verify=ON (CA bundle: {ca})"
    elif not is_https:
        tls = "n/a (plain http)"
    elif _verify_on():
        tls = "verify=ON (system trust store)"
    else:
        tls = "verify=OFF (relaxed for internal hosts only)"

    print("AIForge connectivity test")
    print(f"  role:      {role}")
    print(f"  base_url:  {base_url}")
    print(f"  ssl:       {tls}")
    print(f"  api_key:   {'set' if (api_key and str(api_key).strip()) else 'none'}")
    print("  probing    {base}/models ...".format(base=base_url.rstrip('/')))

    from aiforge_core.llm.providers.openai_compatible import probe
    result = probe(base_url, api_key)
    if result.get("ok"):
        models = result.get("models") or []
        shown = ", ".join(models[:8]) + ("…" if len(models) > 8 else "")
        print(f"\nOK — endpoint reachable, TLS accepted. {len(models)} model(s)"
              + (f": {shown}" if models else "."))
        return 0

    print(f"\nFAIL — {result.get('error', 'unknown error')}")
    err = str(result.get("error", "")).lower()
    if "certificate" in err or "ssl" in err or "verify failed" in err:
        print("  hint: TLS rejected. For a self-hosted box with a self-signed")
        print("        cert, set AIFORGE_LLM_CA_BUNDLE=/path/to/ca.pem (keeps")
        print("        verification ON) or AIFORGE_LLM_SSL_VERIFY=false (relaxes")
        print("        verification for internal hosts only). Put either in")
        print("        your .env so ./run.sh picks it up.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
