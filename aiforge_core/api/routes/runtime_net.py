"""Network settings endpoints: the CA bundle, the egress allow-list, and the
folders mounted into the sandbox."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


class EgressHostsBody(BaseModel):
    extra_hosts: list[str] = Field(
        default_factory=list,
        description="Hosts to allow in ADDITION to the configured integrations. "
                    "A full URL or a bare host[:port] both work; the host is "
                    "what gets stored.")


class CaBundleBody(BaseModel):
    pem: str = Field("", description="PEM text of the CA certificate(s)")


@router.get("/api/runtime/ca")
def ca_get() -> dict:
    """The certificate authority this box trusts, and where it came from.

    Shows the parsed subject, issuer, expiry and fingerprint rather than the
    PEM: a pasted certificate is unreadable to a human, so the screen has to
    prove the right one landed.
    """
    from aiforge_core.net import ca

    return ca.status()


@router.put("/api/runtime/ca", responses={400: {"description": "Bad request"}})
def ca_put(body: CaBundleBody) -> dict:
    """Trust a locally issued CA, from the screen rather than a unit file.

    Everything picks it up at once — the model endpoint, Jira, Confluence,
    GitLab, our own HTTP, and git, curl and npm through the environment — and
    it takes hold immediately, so the request that just failed with
    CERTIFICATE_VERIFY_FAILED can simply be retried.
    """
    from aiforge_core.net import ca

    if len(body.pem) > 512_000:
        raise HTTPException(status_code=400, detail="certificate too large")
    try:
        certs = ca.save(body.pem)
    except ValueError as exc:
        # A bad paste is the operator's typo, not a server fault, and saying
        # which line is wrong beats a generic 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "saved": len(certs), **ca.status()}


@router.post("/api/runtime/ca", responses={400: {"description": "Bad request"}})
def ca_add(body: CaBundleBody) -> dict:
    """ADD certificates, keeping the ones already trusted.

    An estate hands out a root and one or two intermediates, usually as
    separate files. PUT replaces, which loses the first file the moment the
    second is uploaded; this appends, and ignores a fingerprint already in the
    bundle rather than stacking it twice.
    """
    from aiforge_core.net import ca

    if len(body.pem) > 512_000:
        raise HTTPException(status_code=400, detail="certificate too large")
    try:
        certs = ca.add(body.pem)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "count": len(certs), **ca.status()}


@router.delete("/api/runtime/ca/{sha256}", responses={
    404: {"description": "No certificate with that fingerprint"}})
def ca_remove_one(sha256: str) -> dict:
    """Drop ONE certificate from the bundle, by fingerprint."""
    from aiforge_core.net import ca

    removed = ca.remove(sha256)
    if not removed:
        raise HTTPException(status_code=404, detail="no such certificate")
    return {"ok": True, "removed": sha256, **ca.status()}


@router.delete("/api/runtime/ca")
def ca_delete() -> dict:
    """Stop trusting the saved certificate. An env-set bundle is untouched."""
    from aiforge_core.net import ca

    removed = ca.clear()
    return {"ok": True, "removed": removed, **ca.status()}


@router.get("/api/runtime/egress_hosts")
def egress_hosts_get() -> dict:
    """What this box may talk to, and where each entry came from.

    Egress enforcement is always on and the list defaults to DENY, so the
    screen has to show the DERIVED entries too — otherwise an operator adds
    their Jira host by hand and is then surprised that deleting it changes
    nothing."""
    from aiforge_core.config import egress_hosts

    return egress_hosts.describe()


@router.put("/api/runtime/egress_hosts",
            responses={400: {"description": "Bad request"}})
def egress_hosts_put(body: EgressHostsBody) -> dict:
    """Replace the operator's extra hosts. Derived entries are NOT editable
    here — they follow the integration config, so the way to remove one is to
    unconfigure the integration rather than to prune a list that will silently
    regrow."""
    from aiforge_core.config import egress_hosts

    if len(body.extra_hosts) > 100:
        raise HTTPException(status_code=400,
                            detail="too many hosts (max 100)")
    for raw in body.extra_hosts:
        if len(str(raw)) > 253:      # max DNS name length
            raise HTTPException(status_code=400,
                                detail=f"host too long: {str(raw)[:40]}…")
    try:
        saved = egress_hosts.set_stored_hosts(body.extra_hosts)
    except ValueError as exc:
        # A rejected shape is the operator's typo, not a server fault — and the
        # message names what is wrong with which entry.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "extra_hosts": saved, **egress_hosts.describe()}


# ─── sandbox mounts ─────────────────────────────────────────────────────


class _MountBody(BaseModel):
    path: str = Field(..., description="absolute host folder to mount (same path inside the box)")


@router.get("/api/runtime/mounts")
def runtime_mounts() -> dict:
    """Host folders the docker-mode sandbox sees, and ones waiting for the next
    ./run.sh (a running container cannot mount into itself)."""
    from aiforge_core.runtime import sandbox_mounts
    return sandbox_mounts.state()


@router.post("/api/runtime/mounts", responses={400: {"description": "Bad request"}})
def runtime_mounts_add(body: _MountBody) -> dict:
    from aiforge_core.runtime import sandbox_mounts
    try:
        sandbox_mounts.add(body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return sandbox_mounts.state()


@router.delete("/api/runtime/mounts")
def runtime_mounts_remove(path: str) -> dict:
    from aiforge_core.runtime import sandbox_mounts
    sandbox_mounts.remove(path)
    return sandbox_mounts.state()
