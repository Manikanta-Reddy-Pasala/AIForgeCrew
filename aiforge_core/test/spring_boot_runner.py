"""Spawn the doer's branch as a real Spring Boot service for live smoke.

Strategy: only the service we're building runs locally. All deps
(Mongo, Redis/Dragonfly, NATS, MongoDbService, PosService, Gateway,
BusinessService) come from the QA cluster via a pre-existing
``aiforge-qa-portforward`` systemd service that holds kubectl
port-forwards on 127.0.0.1.

The runner overrides Spring property hosts via ``SPRING_APPLICATION_JSON``
so the QA profile yaml's `*.svc.cluster.local` URLs resolve to the
local port-forwards. We don't edit the worktree's yaml — that would
pollute the doer's diff. Env beats yaml in Spring's property
resolution order.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.request
import urllib.error
from dataclasses import dataclass

from aiforge_core.runtime.logging_setup import emit


# Cluster-internal hostnames used in application-qa.yaml → local PF port.
# Keep in sync with scripts/runtime/aiforge-qa-portforward.sh.
_HOST_OVERRIDES: dict[str, dict] = {
    "spring.data.redis.host": "127.0.0.1",
    "spring.data.redis.port": 6379,
    "spring.mongodb.uri": (
        "mongodb://databaseAdmin:Mg%239vB%40kN3wQ5z@127.0.0.1:27017/"
        "oneshell?ssl=false&authSource=admin&directConnection=true"
    ),
    "nats.local.url": "nats://127.0.0.1:4222",
    "mongoDbService.contact-point": "http://127.0.0.1:8080",
    "posService.contact-point": "http://127.0.0.1:8081",
    "gatewayService.contact-point": "http://127.0.0.1:9090",
    "businessservice.contact-point": "http://127.0.0.1:8092",
    # Disable sync push/pull to keep startup quick — we're smoke-testing
    # the doer's new endpoint, not the full sync stack.
    "feature.serverSync.push.enabled": False,
    "feature.serverSync.pull.enabled": False,
    # Ditch graceful shutdown for fast teardown between tickets.
    "spring.lifecycle.timeout-per-shutdown-phase": "5s",
    # Pin to :8090 — the integration runner's curl assumes this.
    "server.port": 8090,
}


@dataclass
class SpringRunResult:
    started: bool = False
    health_ok: bool = False
    pid: int | None = None
    startup_s: float = 0.0
    note: str = ""


def _wait_for_health(base_url: str, timeout_s: int = 180,
                     log: object | None = None) -> tuple[bool, float]:
    """Poll /actuator/health until UP or timeout. Returns (ok, elapsed)."""
    t0 = time.time()
    url = base_url.rstrip("/") + "/actuator/health"
    while time.time() - t0 < timeout_s:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                if '"UP"' in body or resp.status == 200:
                    return True, time.time() - t0
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(2)
    return False, time.time() - t0


def _to_nested(flat: dict) -> dict:
    """Convert {"a.b.c": v} → {"a":{"b":{"c": v}}}.

    Spring Boot 4.x parses SPRING_APPLICATION_JSON as a JSON object;
    flat dot-notation keys are taken at face value (not exploded into
    a nested map), so they don't bind to ``${a.b.c}`` placeholders.
    Build the nested form and let Spring's tree-walk match the yaml
    layout exactly.
    """
    out: dict = {}
    for key, val in flat.items():
        node = out
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = val
    return out


def start_service(worktree: str, log: object | None = None,
                  port: int = 8090,
                  startup_timeout_s: int = 240) -> tuple[subprocess.Popen | None, SpringRunResult]:
    """Boot the doer's worktree as a Spring Boot service.

    Caller is responsible for stopping the returned ``Popen`` via
    :func:`stop_service`.

    Uses ``mvn spring-boot:run`` with ``-DskipTests`` and overrides
    cluster URLs via ``SPRING_APPLICATION_JSON`` (env). Returns
    ``(popen, result)`` — ``popen`` is None on launch failure.
    """
    res = SpringRunResult()
    flat = dict(_HOST_OVERRIDES)
    flat["server.port"] = port
    env = os.environ.copy()
    env["SPRING_PROFILES_ACTIVE"] = "qa"
    env["SPRING_APPLICATION_JSON"] = json.dumps(_to_nested(flat))
    env["JAVA_TOOL_OPTIONS"] = "-Xmx2g -Xms512m -XX:+UseG1GC"

    log_path = os.path.join(
        os.environ.get("AIFORGE_HOME", "/home/mani/.aiforge"),
        "logs", f"spring-boot-{os.path.basename(worktree)}.log",
    )
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log_fh = open(log_path, "wb", buffering=0)

    cmd = ["mvn", "spring-boot:run", "-DskipTests", "-q",
           "-Dspring-boot.run.fork=false"]
    emit(log, "spring_boot_runner.start", worktree=worktree, port=port,
         log_path=log_path)
    try:
        proc = subprocess.Popen(
            cmd, cwd=worktree, env=env,
            stdout=log_fh, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
    except FileNotFoundError as exc:
        res.note = f"mvn not found: {exc}"
        emit(log, "spring_boot_runner.failed", err=res.note)
        return None, res

    res.pid = proc.pid
    res.started = True

    base_url = f"http://127.0.0.1:{port}"
    ok, elapsed = _wait_for_health(base_url, timeout_s=startup_timeout_s,
                                   log=log)
    res.health_ok = ok
    res.startup_s = round(elapsed, 1)
    emit(log, "spring_boot_runner.health",
         ok=ok, startup_s=res.startup_s, pid=proc.pid)
    if not ok:
        # Don't kill — caller may want to inspect. They'll call
        # stop_service when they're done with the smoke probe (or even
        # tail the log to see why startup hung).
        res.note = "actuator/health did not return UP within timeout"
    return proc, res


def stop_service(proc: subprocess.Popen | None,
                 log: object | None = None,
                 grace_s: int = 10) -> None:
    """Send SIGTERM to the spring-boot process group, escalate to KILL."""
    if proc is None or proc.poll() is not None:
        return
    pgid = os.getpgid(proc.pid) if proc.pid else None
    try:
        if pgid:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    t0 = time.time()
    while proc.poll() is None and time.time() - t0 < grace_s:
        time.sleep(0.5)
    if proc.poll() is None:
        try:
            if pgid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
    emit(log, "spring_boot_runner.stopped", pid=proc.pid,
         duration_s=round(time.time() - t0, 1))


__all__ = ["SpringRunResult", "start_service", "stop_service",
           "_wait_for_health"]
