"""Per-repo configuration — `.aiforge/codemem.yaml`.

A single yaml at the repo root drives everything. Env vars are fallbacks
for values not set in the yaml. Defaults are baked in.

Schema:
    repo:
      name: <str>            # logical repo name (becomes Repo.name)
      path: <str>            # absolute path on the host

    knowledge:
      readmes: [list of str]      # additional README/RUNBOOK/ARCHITECTURE files
      conventions: [list of str]  # coding convention docs
      exclude: [list of glob]     # extra ignore patterns (added to defaults)

    services_yaml: <str>     # path to operator-curated services.yaml

    ingest:
      skip_services: bool
      skip_symbols: bool
      skip_summaries: bool
      skip_chunks: bool
      file_summary_max_bytes: int
      embed_max_bytes: int

    llm:
      url: <str>             # OpenAI-compat endpoint
      model: <str>
      api_key: <str>
      repo_summary_max_tokens: int

    embed:
      url: <str>             # bge-m3 sidecar

    neo4j:
      uri: <str>
      user: <str>
      password: <str>

All fields optional. Anything missing falls back to env vars then defaults.

Public:
    cfg = RepoConfig.load(repo_path)         # auto-loads .aiforge/codemem.yaml
    cfg = RepoConfig.load(repo_path, name="...", path="...")  # explicit
    cfg.apply_to_env()                        # exports env vars so existing
                                              # modules pick up overrides
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class RepoConfig:
    name: str = ""
    path: str = ""

    readmes: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    services_yaml: str = ".aiforge/services.yaml"

    skip_services: bool = False
    skip_symbols: bool = False
    skip_summaries: bool = False
    skip_chunks: bool = False
    file_summary_max_bytes: int = 32_768
    embed_max_bytes: int = 65_536

    llm_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    repo_summary_max_tokens: int = 8000

    embed_url: str = ""

    neo4j_uri: str = ""
    neo4j_user: str = ""
    neo4j_password: str = ""

    # ---- loaders -------------------------------------------------------

    @classmethod
    def load(
        cls,
        repo_path: str | Path,
        *,
        name: str | None = None,
    ) -> RepoConfig:
        """Load `.aiforge/codemem.yaml` from `repo_path`. Falls back to
        env vars + defaults for anything not set in the yaml.

        `name` argument overrides the yaml `repo.name` when given.
        """
        repo_path = Path(repo_path).resolve()
        yaml_path = repo_path / ".aiforge" / "codemem.yaml"

        data: dict = {}
        if yaml_path.is_file():
            try:
                data = yaml.safe_load(yaml_path.read_text()) or {}
            except yaml.YAMLError:
                data = {}

        repo = data.get("repo") or {}
        knowledge = data.get("knowledge") or {}
        ingest = data.get("ingest") or {}
        llm = data.get("llm") or {}
        embed = data.get("embed") or {}
        neo4j = data.get("neo4j") or {}

        # Same env fallback chain as every other Neo4j entrypoint.
        from aiforge_memory.core.neo4j import neo4j_settings
        env_uri, env_user, env_pw = neo4j_settings()

        cfg = cls(
            name=name or str(repo.get("name") or repo_path.name),
            path=str(repo.get("path") or repo_path),
            readmes=[str(x) for x in (knowledge.get("readmes") or [])],
            conventions=[str(x) for x in (knowledge.get("conventions") or [])],
            exclude=[str(x) for x in (knowledge.get("exclude") or [])],
            services_yaml=str(data.get("services_yaml") or ".aiforge/services.yaml"),
            skip_services=bool(ingest.get("skip_services", False)),
            skip_symbols=bool(ingest.get("skip_symbols", False)),
            skip_summaries=bool(ingest.get("skip_summaries", False)),
            skip_chunks=bool(ingest.get("skip_chunks", False)),
            file_summary_max_bytes=int(ingest.get("file_summary_max_bytes",
                                                  cls.file_summary_max_bytes)),
            embed_max_bytes=int(ingest.get("embed_max_bytes",
                                           cls.embed_max_bytes)),
            llm_url=str(llm.get("url") or os.environ.get(
                "AIFORGE_CODEMEM_LM_URL",
                os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
            )),
            llm_model=str(llm.get("model") or os.environ.get(
                "AIFORGE_CODEMEM_LM_MODEL", "qwen3.6-27b-instruct",
            )),
            llm_api_key=str(llm.get("api_key") or os.environ.get(
                "AIFORGE_CODEMEM_LM_KEY", "lm-studio",
            )),
            repo_summary_max_tokens=int(llm.get(
                "repo_summary_max_tokens",
                int(os.environ.get("AIFORGE_CODEMEM_REPO_SUMMARY_MAX_TOKENS", 8000)),
            )),
            embed_url=str(embed.get("url") or os.environ.get(
                "AIFORGE_EMBED_URL", "http://127.0.0.1:8764",
            )),
            neo4j_uri=str(neo4j.get("uri") or env_uri),
            neo4j_user=str(neo4j.get("user") or env_user),
            neo4j_password=str(neo4j.get("password") or env_pw),
        )
        return cfg

    # ---- export to env -------------------------------------------------

    def apply_to_env(self) -> None:
        """Export config values as env vars so that legacy modules
        (which read env on import) pick them up. Idempotent."""
        env_map = {
            "AIFORGE_CODEMEM_LM_URL": self.llm_url,
            "AIFORGE_CODEMEM_LM_MODEL": self.llm_model,
            "AIFORGE_CODEMEM_LM_KEY": self.llm_api_key,
            "AIFORGE_CODEMEM_REPO_SUMMARY_MAX_TOKENS":
                str(self.repo_summary_max_tokens),
            "AIFORGE_CODEMEM_FILE_SUMMARY_MAX_BYTES":
                str(self.file_summary_max_bytes),
            "AIFORGE_CODEMEM_EMBED_MAX_BYTES": str(self.embed_max_bytes),
            "AIFORGE_EMBED_URL": self.embed_url,
            "AIFORGE_NEO4J_URI": self.neo4j_uri,
            "AIFORGE_NEO4J_USER": self.neo4j_user,
            "AIFORGE_NEO4J_PASSWORD": self.neo4j_password,
        }
        for k, v in env_map.items():
            if v:
                os.environ[k] = v
