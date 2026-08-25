"""Portable convergence to the zero-Docker SQLite/OKR stack.

Pure Python + the ``docker`` CLI (via subprocess), so it runs IDENTICALLY on
Linux, macOS, WSL and Windows — not just from run.sh's bash. run.sh (or a
Windows launcher, or the API on boot) calls ``converge()``; the logic lives here
once.

Flow — idempotent, marker-guarded, DATA-SAFE:
  1. detect a prior ``aiforge-postgres`` container
  2. start postgres (to read from)
  3. migrate Postgres chat+tickets → SQLite         (scripts/migrate_to_sqlite)
  4. VERIFY it succeeded
  5. ONLY then remove the DB-infra containers + their images + volumes
     — this also tears down any lingering ``aiforge-neo4j`` container from a
       prior hybrid setup. KEEP langfuse (its own postgres powers the trace UI)
  6. clear a stale AIFORGE_PG_URL from .env, mark done (retry next run on failure)

Nothing here raises; every step soft-fails and is logged.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from aiforge_core.config.paths import config_dir

log = logging.getLogger("aiforge.deploy.converge")

# The DB-infra we remove. langfuse (aiforge-langfuse-*) is deliberately NOT here.
_INFRA = ("aiforge-neo4j", "aiforge-embed", "aiforge-rerank", "aiforge-postgres")
_MARKER_NAME = ".data_migrated_v1"
_SUDO: list | None = None


def _config_dir() -> Path:
    return Path(str(config_dir()))


def _marker() -> Path:
    return _config_dir() / _MARKER_NAME


def _repo_root() -> Path:
    # aiforge_core/deploy/converge.py → repo root is two parents up
    return Path(__file__).resolve().parents[2]


def _mark_done() -> None:
    try:
        import time
        _config_dir().mkdir(parents=True, exist_ok=True)
        _marker().write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    except Exception:  # noqa: BLE001
        pass


def _docker(*args: str, timeout: int = 120) -> tuple[int, str]:
    """Run a docker command; return (returncode, stdout). Uses sudo iff the
    daemon needs it. (127, "") when docker isn't installed."""
    global _SUDO
    if not shutil.which("docker"):
        return (127, "")
    if _SUDO is None:
        _SUDO = []
        try:
            if subprocess.run(["docker", "info"], capture_output=True,
                              timeout=15).returncode != 0 and shutil.which("sudo"):
                _SUDO = ["sudo"]
        except Exception:  # noqa: BLE001
            _SUDO = []
    try:
        r = subprocess.run([*_SUDO, "docker", *args], capture_output=True,
                           text=True, timeout=timeout)
        return (r.returncode, (r.stdout or "").strip())
    except Exception as exc:  # noqa: BLE001
        return (1, str(exc))


def _docker_ok() -> bool:
    return _docker("info", timeout=15)[0] == 0


def _container_exists(name: str) -> bool:
    rc, out = _docker("ps", "-aq", "-f", f"name=^{name}$")
    return rc == 0 and bool(out.strip())


def _pg_url() -> str:
    """DSN for the OLD dockerized ``aiforge-postgres`` we migrate OFF of.

    Inlined (the shared config.env DSN helper was removed with Postgres) so
    this one-time migration-read path carries no dependency on the deleted
    helper. NO baked-in credential: the password comes only from the
    environment; with none set the DSN is user-only — which is what the local
    docker Postgres (loopback, trust/peer auth) wants and is honest about
    carrying no secret.
    """
    if os.environ.get("AIFORGE_PG_URL"):
        return os.environ["AIFORGE_PG_URL"]
    user = os.environ.get("AIFORGE_PG_USER") or os.environ.get("USER") or "aiforge"
    pwd = os.environ.get("AIFORGE_PG_PASSWORD") or ""
    host = os.environ.get("AIFORGE_PG_HOST", "127.0.0.1")
    port = os.environ.get("AIFORGE_PG_PORT", "5432")
    db = os.environ.get("AIFORGE_PG_DB", "aiforge")
    auth = f"{user}:{pwd}" if pwd else user
    return f"postgresql://{auth}@{host}:{port}/{db}"


def _migrate_pg_to_sqlite() -> bool:
    """Run scripts/migrate_to_sqlite (chat + tickets) as a subprocess with the
    source PG url + lite mode. True on exit 0."""
    env = dict(os.environ, AIFORGE_PG_URL=_pg_url(), AIFORGE_MODE="lite")
    script = _repo_root() / "scripts" / "migrate_to_sqlite.py"
    try:
        r = subprocess.run([sys.executable, str(script)], env=env,
                           cwd=str(_repo_root()), timeout=600)
        return r.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log.warning("converge: migrate_to_sqlite failed: %s", exc)
        return False


def _remove_infra_containers() -> list[str]:
    """Force-remove the explicit DB-infra containers; return the names removed."""
    return [name for name in _INFRA
            if _container_exists(name) and _docker("rm", "-f", name)[0] == 0]


def _remove_aiforge_volumes() -> list[str]:
    """Remove aiforge-named volumes, NEVER langfuse; return the names removed."""
    rc, out = _docker("volume", "ls", "-q")
    if rc != 0:
        return []
    removed = []
    for v in out.splitlines():
        v = v.strip()
        if v and "aiforge" in v.lower() and "langfuse" not in v.lower():
            if _docker("volume", "rm", v)[0] == 0:
                removed.append(v)
    return removed


def _remove_aiforge_images() -> list[str]:
    """Remove aiforge-tagged images, NEVER langfuse; return the image ids
    removed (deduped, since one id can carry several repo:tag lines)."""
    rc, out = _docker("images", "--filter", "reference=*aiforge*",
                      "--format", "{{.Repository}}:{{.Tag}}|{{.ID}}")
    if rc != 0:
        return []
    removed, seen = [], set()
    for line in out.splitlines():
        repo_tag, _, img_id = line.partition("|")
        if "langfuse" in repo_tag.lower() or not img_id or img_id in seen:
            continue
        seen.add(img_id)
        if _docker("rmi", "-f", img_id)[0] == 0:
            removed.append(img_id)
    return removed


def _remove_db_infra() -> dict:
    """Remove the DB-infra containers + their images + volumes. NEVER langfuse."""
    return {"containers": _remove_infra_containers(),
            "volumes": _remove_aiforge_volumes(),
            "images": _remove_aiforge_images()}


# Backend-pointer keys that belong to the removed hybrid mode. This build is
# SQLite-only; neutralise them wherever they linger so nothing tries a gone PG.
_DB_KEY_RE = re.compile(
    r"^\s*(AIFORGE_PG_URL|AIFORGE_DSN|AIFORGE_PGMEM_DSN|AIFORGE_FORCE_PG|"
    r"AIFORGE_MEMORY_BACKEND|AIFORGE_NEO4J_URI|NEO4J_URI|AIFORGE_NEO4J_USER|"
    r"AIFORGE_NEO4J_PASSWORD|AIFORGE_NEO4J_PASS|AIFORGE_REQUIRE_DATA_BACKEND)\s*=")


def _neutralise_db_lines(path) -> bool:
    """Comment out stale PG/Neo4j backend lines in one env file. True when it
    changed. Best-effort — an unreadable/unwritable file is skipped."""
    if not path.is_file():
        return False
    try:
        out, changed = [], False
        for ln in path.read_text(encoding="utf-8").splitlines():
            if _DB_KEY_RE.match(ln) and not ln.lstrip().startswith("#"):
                out.append("# [converge→sqlite] " + ln)
                changed = True
            else:
                out.append(ln)
        if changed:
            path.write_text("\n".join(out) + "\n", encoding="utf-8")
            log.info("converge: neutralised PG/Neo4j lines in %s", path.name)
        return changed
    except Exception:  # noqa: BLE001
        return False


def _clear_pg_from_env() -> None:
    """Comment out stale Postgres/Neo4j backend lines in the repo .env AND the
    UI-persisted ~/.aiforge/runtime.env, so a future boot never re-points the
    SQLite-only app at a removed Postgres/Neo4j."""
    for path in (_repo_root() / "aiforge.env", _repo_root() / ".env",
                 _config_dir() / "runtime.env"):
        _neutralise_db_lines(path)

def converge(*, force: bool = False) -> dict:
    """Run the convergence once. Returns a summary; never raises."""
    if os.environ.get("AIFORGE_AUTO_MIGRATE", "1").lower() in ("0", "false", "no"):
        return {"skipped": "disabled"}
    _clear_pg_from_env()               # SQLite-only: neutralise stale PG/Neo4j env always
    if _marker().exists() and not force:
        # already migrated → but if DB-infra containers still linger (a prior run
        # only stopped them, or docker came up again), remove them now: the data
        # is already on SQLite/OKR (marker proves it). langfuse untouched.
        if _docker_ok() and any(_container_exists(n) for n in _INFRA):
            removed = _remove_db_infra()
            _clear_pg_from_env()
            log.info("converge: removed lingering DB-infra (data already migrated)")
            return {"ok": True, "cleaned": removed}
        return {"skipped": "already done"}
    if not _docker_ok():
        _mark_done()                       # no docker → nothing to converge
        return {"skipped": "no docker"}
    if not _container_exists("aiforge-postgres"):
        _mark_done()                       # no prior PG → nothing to migrate
        return {"skipped": "no aiforge-postgres"}

    log.info("converge: prior dockerized Postgres detected — migrating to SQLite/OKR")
    _docker("start", "aiforge-postgres")
    import time
    time.sleep(6)

    if not _migrate_pg_to_sqlite():
        log.error("converge: Postgres→SQLite FAILED — keeping Docker intact (retry next run)")
        return {"ok": False, "step": "postgres_to_sqlite"}

    log.info("converge: migration verified — removing DB-infra (KEEPING langfuse)")
    removed = _remove_db_infra()
    _clear_pg_from_env()
    _mark_done()
    log.info("converge: done — removed %d containers / %d images / %d volumes",
             len(removed["containers"]), len(removed["images"]), len(removed["volumes"]))
    return {"ok": True, "removed": removed}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(converge(force="--force" in sys.argv))
