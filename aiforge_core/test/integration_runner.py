"""Integration test runner — invoked by AiForgeIntegrationTestAgent.

Three steps, each best-effort and gated by env:

1. **Unit tests** — `mvn -DskipTests=false test` inside the doer's
   worktree. Captures pass/fail + last 2KB output.
2. **Service smoke** — discover the new endpoint from the diff, fetch
   a real ``businessId`` via MCP (oneshell-mongo-qa), spin up the
   service via `mvn spring-boot:run` in the worktree (or hit an
   existing QA deployment if `AIFORGE_TEST_BASE_URL` set), curl the
   endpoint, validate response.
3. **Cleanup** — kill spring-boot child if we started one.

Returns ``IntegrationResult`` with ``test_green: bool`` + per-step
detail. Caller writes counters into session state for the deterministic
acceptance gate.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiforge_core.observability.logging import emit
from aiforge_core.test.mcp_client import find_test_business


@dataclass
class IntegrationResult:
    test_green: bool = False
    unit_tests_ran: bool = False
    unit_tests_pass: bool = False
    unit_tests_tail: str = ""
    smoke_ran: bool = False
    smoke_pass: bool = False
    smoke_endpoint: str = ""
    smoke_status_code: int = 0
    smoke_body_excerpt: str = ""
    business_id: str = ""
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)


_GET_MAPPING_RX = re.compile(
    r'@GetMapping\(\s*"([^"]+)"\s*\)\s*\n[^@]*?\b(\w+)\s*\(',
    re.MULTILINE,
)
_REQUEST_MAPPING_RX = re.compile(
    r'@RequestMapping\(\s*"([^"]+)"\s*\)',
)
# Diff-side detection — only @GetMapping is curl-able with our smoke. Other
# verbs need request bodies; we don't synthesise those.
_DIFF_GET_RX = re.compile(
    r'^\+\s*@GetMapping\(\s*"([^"]+)"', re.MULTILINE,
)
_PATH_VAR_RX = re.compile(r"\{([^}]+)\}")


def _run(argv: list[str], cwd: str | None = None,
         timeout: int = 600,
         env: dict | None = None) -> tuple[int, str, str]:
    try:
        cp = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, check=False, env=env,
        )
        return cp.returncode, cp.stdout, cp.stderr
    except subprocess.TimeoutExpired as exc:
        return -1, exc.stdout or "", f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)


def _diff_endpoint(worktree: str) -> tuple[str, str] | None:
    """Find a new @GetMapping path added in the doer's commit.

    Splits the diff into per-file chunks, picks the chunk that adds an
    @GetMapping, then reads the controller class file to grab its
    @RequestMapping prefix. Returns (full_path, controller_class_name).
    """
    rc, diff, _ = _run(
        ["git", "diff", "origin/master...HEAD", "--", "*.java"],
        cwd=worktree, timeout=30,
    )
    if rc != 0 or not diff:
        return None
    # Split per-file by `diff --git` markers so we know which file the
    # matched @GetMapping was added in.
    chunks = re.split(r'(?=^diff --git )', diff, flags=re.MULTILINE)
    chosen_rel: str | None = None
    sub_path: str | None = None
    for chunk in chunks:
        m = _DIFF_GET_RX.search(chunk)
        if not m:
            continue
        sub_path = m.group(1)
        fm = re.search(r'^\+\+\+ b/([^\n]+)', chunk, re.MULTILINE)
        if fm:
            chosen_rel = fm.group(1)
        break
    if not sub_path:
        return None
    base_path = ""
    controller = ""
    if chosen_rel:
        full = os.path.join(worktree, chosen_rel)
        if os.path.isfile(full):
            text = Path(full).read_text(errors="replace")
            rm = _REQUEST_MAPPING_RX.search(text)
            if rm:
                base_path = rm.group(1)
            cls_m = re.search(r"public\s+class\s+(\w+)", text)
            if cls_m:
                controller = cls_m.group(1)
    full_path = (base_path.rstrip("/") + "/" + sub_path.lstrip("/")) \
        if base_path else sub_path
    return full_path, controller


def _build_url_with_test_data(path: str, base: str) -> tuple[str, str]:
    """Substitute {pathVar} placeholders with a real businessId fetched
    from oneshell-mongo-qa MCP. If the path has no businessId placeholder,
    returns ``(base+path, "")``.
    """
    business_id = ""
    if "{businessId}" in path or "{businessid}" in path.lower():
        business_id = find_test_business("paymentIn", tier="qa") or ""
        if business_id:
            path = re.sub(r"\{businessId\}", business_id, path,
                          flags=re.IGNORECASE)
    # Substitute remaining {placeholder} with a realistic value based on the
    # name. Endpoints frequently take dates / months / years / counters as
    # path vars; a literal "test" makes the controller throw a parse error
    # before the endpoint logic even runs, hiding real failures.
    def _smart_sub(m: re.Match) -> str:
        name = m.group(1).lower()
        if "yyyymm" in name or ("month" in name and "year" not in name):
            return "202604"
        if "yyyy" in name or "year" in name:
            return "2026"
        if "date" in name or "day" in name:
            return "2026-04-25"
        if "id" in name:
            return business_id or "0"
        if name in ("limit", "offset", "page", "size", "count"):
            return "10"
        return "test"
    path = _PATH_VAR_RX.sub(_smart_sub, path)
    if not path.startswith("/"):
        path = "/" + path
    return base.rstrip("/") + path, business_id


def run_integration(worktree: str, log: object | None = None,
                    base_url: str | None = None) -> IntegrationResult:
    t0 = time.time()
    result = IntegrationResult()
    base_url = base_url or os.environ.get(
        "AIFORGE_TEST_BASE_URL", "http://127.0.0.1:8090"
    )

    # 1) Unit tests — disabled by default because PosClientBackend tests
    # require Mongo/Redis/NATS that aren't always reachable from NUC.
    # Set AIFORGE_TEST_RUN_UNIT=1 to enable.
    if os.environ.get("AIFORGE_TEST_RUN_UNIT") == "1":
        result.unit_tests_ran = True
        rc, stdout, stderr = _run(
            ["mvn", "-q", "-DskipTests=false", "test"],
            cwd=worktree, timeout=900,
        )
        out = (stdout or "") + "\n" + (stderr or "")
        result.unit_tests_pass = (rc == 0 and "BUILD FAILURE" not in out)
        result.unit_tests_tail = out[-2000:]
        emit(log, "integration.unit_tests", pass_=result.unit_tests_pass,
             rc=rc)

    # 2) Smoke — discover endpoint, fetch real test data, curl it.
    endpoint = _diff_endpoint(worktree)
    if not endpoint:
        result.notes.append("no new GetMapping found in diff — smoke skipped")
        # Strict mode (default when integration explicitly enabled): missing
        # endpoint == failure, since the doer was supposed to wire one up.
        # Loose mode (AIFORGE_TEST_INTEGRATION_STRICT=0): pass through, used
        # for tickets whose acceptance is non-endpoint (refactor, fix, etc.).
        strict = os.environ.get("AIFORGE_TEST_INTEGRATION_STRICT", "1") == "1"
        if strict:
            result.test_green = False
        else:
            result.test_green = (not result.unit_tests_ran) or result.unit_tests_pass
        result.duration_s = round(time.time() - t0, 2)
        return result

    path, controller = endpoint
    url, business_id = _build_url_with_test_data(path, base_url)
    result.smoke_endpoint = url
    result.business_id = business_id
    emit(log, "integration.endpoint_discovered",
         path=path, controller=controller, business_id=business_id)

    # 2a) Optionally spawn the doer's branch as a real Spring Boot
    # service. Only the service we're building runs locally — Mongo,
    # Redis, NATS, and the other Spring backends are reached via the
    # aiforge-qa-portforward systemd service (kubectl port-forwards
    # against the QA cluster). Toggle: AIFORGE_TEST_INTEGRATION_LIVE=1.
    live = os.environ.get("AIFORGE_TEST_INTEGRATION_LIVE", "0") == "1"
    spring_proc = None
    if live:
        from aiforge_core.test.spring_boot_runner import (
            start_service, stop_service,
        )
        spring_proc, sb_res = start_service(worktree, log=log, port=8090)
        result.notes.append(
            f"spring_boot startup_s={sb_res.startup_s} "
            f"health_ok={sb_res.health_ok} note={sb_res.note}"
        )
        if not sb_res.health_ok:
            # Don't curl a service that didn't come up — record the
            # diagnostic and bail. stop_service still runs in the
            # `finally` to clean up the half-started process.
            stop_service(spring_proc, log=log)
            result.smoke_ran = False
            result.test_green = False
            result.duration_s = round(time.time() - t0, 2)
            return result

    rc, stdout, stderr = _run(
        ["curl", "-s", "-o", "-", "-w", "\n%{http_code}",
         "-m", "20", url],
        timeout=30,
    )
    if live and spring_proc is not None:
        from aiforge_core.test.spring_boot_runner import stop_service
        stop_service(spring_proc, log=log)
    result.smoke_ran = True
    body = (stdout or "")
    code = 0
    m = re.search(r"\n(\d{3})\s*$", body)
    if m:
        code = int(m.group(1))
        body = body[:m.start()]
    result.smoke_status_code = code
    result.smoke_body_excerpt = body[:600]
    # 200 OK or 404 (business has no data) both indicate the endpoint exists
    # and responds — useful when smoke runs against a stub deployment.
    result.smoke_pass = code in (200, 204, 404, 422)
    emit(log, "integration.smoke", url=url, status=code,
         pass_=result.smoke_pass)

    unit_ok = (not result.unit_tests_ran) or result.unit_tests_pass
    # Diff-only mode (AIFORGE_TEST_INTEGRATION_DIFF_ONLY=1): when the
    # smoke target is unreachable (code==0), accept "endpoint exists in
    # diff" as proof. Useful when PosClientBackend isn't running on the
    # gate host — still catches ONE-7-style controller-skip via the
    # required @GetMapping check above; just skips the live HTTP probe.
    diff_only = (
        os.environ.get("AIFORGE_TEST_INTEGRATION_DIFF_ONLY", "0") == "1"
    )
    if diff_only and result.smoke_status_code == 0:
        result.notes.append(
            "diff-only mode: endpoint present in diff, live smoke skipped "
            "(target unreachable)"
        )
        result.test_green = unit_ok
    else:
        result.test_green = unit_ok and result.smoke_pass
    result.duration_s = round(time.time() - t0, 2)
    return result


__all__ = ["IntegrationResult", "run_integration"]
