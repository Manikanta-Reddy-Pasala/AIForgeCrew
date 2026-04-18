"""Config loader for paperclip.config.yml + agent permissions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AgentBudget:
    role: str
    tokens_per_ticket: int
    cloud_usd_per_month: float | None = None


@dataclass(frozen=True)
class RetryRules:
    dev_tester_loops_max: int
    dev_architect_loops_max: int
    hermes_checkpoint_every_n_calls: int
    stale_ticket_timeout_minutes: int


@dataclass(frozen=True)
class Routing:
    initial_assignee: str
    post_planning: str
    post_tests_ready: str
    post_code_ready: str
    post_verified: str
    on_approve: str  # "human" means kick back to CEO, not merge


@dataclass(frozen=True)
class AuditCfg:
    append_only: bool
    single_ticket_thread: bool
    log_path: Path


@dataclass(frozen=True)
class PaperclipConfig:
    org_chart: dict[str, dict[str, Any]]
    budgets: dict[str, AgentBudget]
    retry_rules: RetryRules
    routing: Routing
    audit: AuditCfg
    repo_root: Path

    @classmethod
    def load(cls, repo_root: Path) -> "PaperclipConfig":
        path = repo_root / "paperclip.config.yml"
        doc = yaml.safe_load(path.read_text())

        budgets = {}
        for role, b in (doc.get("budgets") or {}).items():
            budgets[role] = AgentBudget(
                role=role,
                tokens_per_ticket=int(b["tokens_per_ticket"]),
                cloud_usd_per_month=b.get("cloud_usd_per_month"),
            )
        retry = doc.get("retry_rules") or {}
        routing = doc.get("routing") or {}
        audit = doc.get("audit") or {}

        return cls(
            org_chart=doc.get("org_chart") or {},
            budgets=budgets,
            retry_rules=RetryRules(
                dev_tester_loops_max=int(retry.get("dev_tester_loops_max", 3)),
                dev_architect_loops_max=int(retry.get("dev_architect_loops_max", 3)),
                hermes_checkpoint_every_n_calls=int(retry.get("hermes_checkpoint_every_n_calls", 15)),
                stale_ticket_timeout_minutes=int(retry.get("stale_ticket_timeout_minutes", 60)),
            ),
            routing=Routing(
                initial_assignee=routing.get("initial_assignee", "engineering_manager"),
                post_planning=routing.get("post_planning", "tester"),
                post_tests_ready=routing.get("post_tests_ready", "sr_developer"),
                post_code_ready=routing.get("post_code_ready", "tester"),
                post_verified=routing.get("post_verified", "sr_architect"),
                on_approve=routing.get("on_approve", "human"),
            ),
            audit=AuditCfg(
                append_only=bool(audit.get("append_only", True)),
                single_ticket_thread=bool(audit.get("single_ticket_thread", True)),
                log_path=repo_root / audit.get("log_path", ".paperclip/audit"),
            ),
            repo_root=repo_root,
        )


def load_permissions(repo_root: Path, role: str) -> dict[str, bool]:
    """Load agents/<role>/permissions.yml → {capability: bool}."""
    path = repo_root / "agents" / role / "permissions.yml"
    doc = yaml.safe_load(path.read_text()) or {}
    return dict(doc.get("can") or {})
