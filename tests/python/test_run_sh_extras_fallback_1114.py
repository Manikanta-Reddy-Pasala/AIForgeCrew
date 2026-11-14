"""run.sh: a semantic memory backend installs its vector extra, and an optional
extra that cannot be fetched never stops the service."""
from __future__ import annotations

import pathlib
import re

SRC = (pathlib.Path(__file__).resolve().parents[2] / "run.sh").read_text(encoding="utf-8")


def test_a_semantic_backend_pulls_the_embed_static_extra():
    block = SRC[SRC.index('case "${AIFORGE_EMBED_BACKEND:-}" in'):]
    block = block[:block.index("esac")]
    assert re.search(r"\bmodel2vec\b", block)
    assert "sidecar" not in block.split(")")[0]      # sidecar embeds elsewhere: no extra
    assert "embed-static" in block


def test_an_optional_extra_failure_falls_back_to_the_base_set():
    assert '_BASE_EXTRAS=(--extra toolchain)' in SRC
    i = SRC.index('if _install_set "${_EXTRAS[@]}"; then')
    tail = SRC[i:i + 1500]
    assert '_install_set "${_BASE_EXTRAS[@]}"' in tail
    assert tail.index('_install_set "${_BASE_EXTRAS[@]}"') < tail.index("_fatal")
    # the base-set fallback writes no stamp, so the next start retries the full set
    fallback = tail[tail.index('_install_set "${_BASE_EXTRAS[@]}"'):tail.index("_fatal")]
    assert "_STAMP" not in fallback


def test_the_container_gets_the_hosts_timezone():
    """The box ran on UTC: the 18:00 evening compaction fired at 22:00 local."""
    assert 'TZ="$(timedatectl show -p Timezone --value' in SRC
    assert "export TZ" in SRC
    compose = (pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    assert re.search(r"^\s+- TZ\b", compose, re.M)


def test_a_failed_install_with_a_working_venv_still_starts():
    i = SRC.index('if _install_set "${_EXTRAS[@]}"; then')
    tail = SRC[i:i + 2200]
    assert "elif _venv_ready; then" in tail
    assert tail.index("elif _venv_ready; then") < tail.index("_fatal")
