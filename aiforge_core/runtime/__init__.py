"""AIForge orchestrator runtime.

Single-Postgres tick-based orchestrator. One tick per role per launchd
timer firing. Per-role fcntl lock, claim_next → tool loop → finalize.
"""
# Auto-load ~/.aiforge/aiforge.yaml (or $AIFORGE_CONFIG) before any
# runtime.config consumer reads env. Env wins over yaml; defaults win
# over yaml when nothing set. Disable via AIFORGE_CONFIG_AUTOLOAD=0.
from aiforge_core.config import yaml as _yaml_config  # noqa: F401
