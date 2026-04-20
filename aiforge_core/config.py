"""Config loader for paperclip.config.yml v4.1."""
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
    review_reject_loops_max: int
    max_steps_per_ticket: int
    max_retries_per_step: int
    tool_timeout_s: int
    llm_request_timeout_s: int
    stale_ticket_timeout_minutes: int


@dataclass(frozen=True)
class Routing:
    initial_assignee_parent: str
    post_architect_planning: str
    post_sr_decomposition:   str
    post_children_merged:    str
    post_reflection:         str
    initial_assignee_child:  str
    post_developer_code:     str
    on_architect_approve:    str
    on_architect_reject:     str


@dataclass(frozen=True)
class Confidence:
    proceed_threshold: float
    retry_threshold: float
    escalate_threshold: float


@dataclass(frozen=True)
class KillSwitch:
    global_file: str
    ticket_tag: str


@dataclass(frozen=True)
class AuditCfg:
    append_only: bool
    single_thread_per_parent: bool
    log_path: Path


@dataclass(frozen=True)
class PaperclipConfig:
    org_chart: dict[str, dict[str, Any]]
    budgets: dict[str, AgentBudget]
    retry_rules: RetryRules
    routing: Routing
    confidence: Confidence
    kill_switch: KillSwitch
    audit: AuditCfg
    repo_root: Path

    @classmethod
    def load(cls, repo_root: Path) -> "PaperclipConfig":
        path = repo_root / "paperclip.config.yml"
        doc = yaml.safe_load(path.read_text())

        budgets: dict[str, AgentBudget] = {}
        for role, b in (doc.get("budgets") or {}).items():
            budgets[role] = AgentBudget(
                role=role,
                tokens_per_ticket=int(b["tokens_per_ticket"]),
                cloud_usd_per_month=b.get("cloud_usd_per_month"),
            )
        r = doc.get("retry_rules") or {}
        rt = doc.get("routing") or {}
        cf = doc.get("confidence") or {}
        ks = doc.get("kill_switch") or {}
        au = doc.get("audit") or {}

        return cls(
            org_chart=doc.get("org_chart") or {},
            budgets=budgets,
            retry_rules=RetryRules(
                review_reject_loops_max=int(r.get("review_reject_loops_max", 3)),
                max_steps_per_ticket=int(r.get("max_steps_per_ticket", 20)),
                max_retries_per_step=int(r.get("max_retries_per_step", 3)),
                tool_timeout_s=int(r.get("tool_timeout_s", 60)),
                llm_request_timeout_s=int(r.get("llm_request_timeout_s", 300)),
                stale_ticket_timeout_minutes=int(r.get("stale_ticket_timeout_minutes", 120)),
            ),
            routing=Routing(
                initial_assignee_parent=rt.get("initial_assignee_parent", "architect"),
                post_architect_planning=rt.get("post_architect_planning", "sr_developer"),
                post_sr_decomposition=rt.get("post_sr_decomposition", "_spawned"),
                post_children_merged=rt.get("post_children_merged", "fact_extract"),
                post_reflection=rt.get("post_reflection", "_closed"),
                initial_assignee_child=rt.get("initial_assignee_child", "developer"),
                post_developer_code=rt.get("post_developer_code", "architect"),
                on_architect_approve=rt.get("on_architect_approve", "_mr_created"),
                on_architect_reject=rt.get("on_architect_reject", "developer"),
            ),
            confidence=Confidence(
                proceed_threshold=float(cf.get("proceed_threshold", 0.70)),
                retry_threshold=float(cf.get("retry_threshold", 0.50)),
                escalate_threshold=float(cf.get("escalate_threshold", 0.30)),
            ),
            kill_switch=KillSwitch(
                global_file=ks.get("global_file", ".aiforge/KILL"),
                ticket_tag=ks.get("ticket_tag", "kill"),
            ),
            audit=AuditCfg(
                append_only=bool(au.get("append_only", True)),
                single_thread_per_parent=bool(au.get("single_thread_per_parent", True)),
                log_path=repo_root / au.get("log_path", ".paperclip/audit"),
            ),
            repo_root=repo_root,
        )


def load_permissions(repo_root: Path, role: str) -> dict[str, bool]:
    path = repo_root / "agents" / role / "permissions.yml"
    doc = yaml.safe_load(path.read_text()) or {}
    return dict(doc.get("can") or {})
