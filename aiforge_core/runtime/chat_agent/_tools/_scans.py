from __future__ import annotations


def _t_sonar_report(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import sonar
    return sonar.sonar_report(args, cwd)


def _t_blackduck_report(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import blackduck
    return blackduck.blackduck_report(args, cwd)
