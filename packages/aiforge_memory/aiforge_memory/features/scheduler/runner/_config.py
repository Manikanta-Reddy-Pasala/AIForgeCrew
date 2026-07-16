"""Config — RepoSchedule / SchedulerConfig and yaml add/remove helpers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from ._paths import CONFIG_PATH


# ─── Config ───────────────────────────────────────────────────────────

@dataclass
class RepoSchedule:
    name: str
    path: str
    interval_seconds: int = 600
    pull: bool = True
    skip_services: bool = False    # skip Stage 3 (LLM service catalog)
    skip_summaries: bool = False
    skip_chunks: bool = False
    use_lsp: bool = False          # opt-in LSP-confirmed CALLS
    timeout_seconds: int = 1800    # FLOOR for the per-tick wall ceiling.
                                   # Effective timeout grows with file
                                   # count when per_file_seconds > 0.
    per_file_seconds: float = 0.0  # 0 = fixed timeout. >0 enables
                                   # dynamic scaling: timeout =
                                   # max(timeout_seconds,
                                   #     file_count × per_file_seconds),
                                   # capped by AIFORGE_SCHEDULER_MAX_TIMEOUT_S
                                   # (default 14400s / 4 hr).


@dataclass
class SchedulerConfig:
    repos: list[RepoSchedule] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> SchedulerConfig:
        path = Path(path or CONFIG_PATH)
        if not path.is_file():
            return cls()
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            return cls()
        repos: list[RepoSchedule] = []
        for r in data.get("repos") or []:
            try:
                repos.append(RepoSchedule(
                    name=str(r["name"]),
                    path=str(r["path"]),
                    interval_seconds=int(r.get("interval_seconds", 600)),
                    pull=bool(r.get("pull", True)),
                    skip_services=bool(r.get("skip_services", False)),
                    skip_summaries=bool(r.get("skip_summaries", False)),
                    skip_chunks=bool(r.get("skip_chunks", False)),
                    use_lsp=bool(r.get("use_lsp", False)),
                    timeout_seconds=int(r.get("timeout_seconds", 1800)),
                    per_file_seconds=float(r.get("per_file_seconds", 0.0)),
                ))
            except (KeyError, ValueError):
                continue
        return cls(repos=repos)

    def save(self, path: Path | None = None) -> None:
        path = Path(path or CONFIG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(
            {"repos": [asdict(r) for r in self.repos]},
            default_flow_style=False, sort_keys=False,
        ))


def add_repo(rs: RepoSchedule, *, path: Path | None = None) -> None:
    cfg = SchedulerConfig.load(path)
    cfg.repos = [r for r in cfg.repos if r.name != rs.name]
    cfg.repos.append(rs)
    cfg.save(path)


def remove_repo(name: str, *, path: Path | None = None) -> bool:
    cfg = SchedulerConfig.load(path)
    before = len(cfg.repos)
    cfg.repos = [r for r in cfg.repos if r.name != name]
    cfg.save(path)
    return len(cfg.repos) < before
