"""What a prediction may do on its own: blast radius, not confidence.

The rule is a TABLE rather than a threshold, and that is the whole point. A
threshold says "act when sure", which is precisely what lets a confident model
push to production — confidence is evidence about whether the guess is RIGHT,
and says nothing at all about what it costs when it is wrong.

Three tiers:

1. **Reversible and local** — reads, searches, a ``SAFE`` shell command. Acting
   on a wrong guess costs a wasted second.
2. **Writes the workspace** — an edit, a commit. Acting on a wrong guess costs
   nothing only while git can undo it, which is why a dirty tree demotes this
   tier: the user's own uncommitted work is mixed in with ours, and
   ``git checkout`` stops being a safe answer to "that was wrong".
3. **Everything else** — leaves the machine, spends money, or cannot be undone.
   No degree of confidence makes an irreversible guess acceptable, so this tier
   is never acted on.

An unrecognised tool falls in tier 3. The unknown case has to be the careful
one, or every tool added after this file is a hole until somebody remembers to
come back and classify it.
"""
from __future__ import annotations

import os

ACT = "ACT"
OFFER = "OFFER"

# Tier 1 — reversible, and nothing outside this machine observes it.
_READ_ONLY = frozenset({
    "read_file", "read", "grep", "search", "list_dir", "ls", "glob",
    "repo_map", "recall", "memory_search", "web_search",
})

# Tier 2 — changes the workspace. Reversible while git can undo it.
_WORKSPACE_WRITE = frozenset({"write_file", "edit_file", "apply_patch", "patch"})

# Shell verbs that leave the machine or cannot be undone, whatever
# ``command_risk`` makes of the rest of the line. Checked as substrings because
# these appear mid-line as often as they start one ("cd x && git push").
_LEAVES_THE_MACHINE = (
    "git push", "git tag", "docker push", "kubectl", "helm", "terraform",
    "aws ", "gcloud ", "az ", "ssh ", "scp ", "rsync ", "npm publish",
    "pip upload", "twine", "systemctl", "reboot", "shutdown", "kill ",
)

# Shell verbs that write the tree without leaving the machine.
_WRITES_THE_TREE = (
    "git commit", "git checkout", "git reset", "git merge", "git rebase",
    "git stash", "git apply", "mv ", "cp ", "mkdir ", "touch ", "chmod ",
)

_DEFAULT_MIN_CONFIDENCE = 0.75


def min_confidence() -> float:
    try:
        return float(os.environ.get("AIFORGE_PREDICT_MIN_CONFIDENCE") or
                     _DEFAULT_MIN_CONFIDENCE)
    except ValueError:
        return _DEFAULT_MIN_CONFIDENCE


def acting_enabled() -> bool:
    """``AIFORGE_PREDICT_ACT=0`` makes every prediction an offer.

    The "always ask me" setting, deliberately separate from the kill switch: an
    operator may well want the suggestions and not want them acted on.
    """
    return (os.environ.get("AIFORGE_PREDICT_ACT") or "1").strip() != "0"


def _shell_command(args: dict) -> str:
    a = args or {}
    return str(a.get("cmd") or a.get("command") or "")


def _mentions(low: str, verbs: tuple) -> bool:
    return any(v in low for v in verbs)


def _shell_tier(cmd: str) -> int:
    """Tier for one shell command. An empty command is tier 3, not tier 1 —
    a prediction that cannot say what it would run is not one to run."""
    from aiforge_core.runtime.tools import command_risk

    low = " ".join(str(cmd or "").lower().split())
    if not low:
        return 3
    if _mentions(low, _LEAVES_THE_MACHINE):
        return 3
    # Consulted rather than duplicated: command_risk already owns "which shell
    # commands are dangerous", and a second list here would drift out of step
    # with it in whichever direction nobody happened to be watching.
    if command_risk.assess(str(cmd or "")).get("level") != command_risk.SAFE:
        return 3
    return 2 if _mentions(low, _WRITES_THE_TREE) else 1


def tier(tool: str, args: dict) -> int:
    """1 = reversible and local, 2 = writes the workspace, 3 = neither."""
    name = str(tool or "").strip()
    if not name:
        return 1                    # a prediction that only says something
    if name in _READ_ONLY:
        return 1
    if name in _WORKSPACE_WRITE:
        return 2
    if name == "bash":
        return _shell_tier(_shell_command(args))
    return 3


def verdict(tool: str, args: dict, *, confidence: float,
            clean_tree: bool) -> str:
    """``ACT`` or ``OFFER`` for one prediction. Never raises.

    Confidence is necessary and never sufficient: it gates tiers 1 and 2, and is
    ignored entirely at tier 3.
    """
    try:
        if not acting_enabled() or confidence < min_confidence():
            return OFFER
        level = tier(tool, args)
    except Exception:  # noqa: BLE001 — an action we cannot classify is not one we take
        return OFFER
    if level == 1:
        return ACT
    if level == 2:
        return ACT if clean_tree else OFFER
    return OFFER


__all__ = ["ACT", "OFFER", "tier", "verdict", "min_confidence", "acting_enabled"]
