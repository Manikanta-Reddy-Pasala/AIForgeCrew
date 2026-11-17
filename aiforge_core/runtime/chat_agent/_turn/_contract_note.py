"""After an edit: when the file just written is a class the request states
signatures for, compare them at once.

A real request said the unit test would call ``new Notifier(String url,
String token)``. Two runs wrote the class with a framework-injected
constructor instead, got stuck elsewhere, gave up, and the finish check that
compares signatures never ran: every hidden test failed to compile. The
gaps are told right after the class is written, while the model is still on
it. Once per distinct set of gaps; typed languages (Java, Kotlin, C#, …) via
runtime/typed_contract. AIFORGE_CHAT_CONTRACT_ON_WRITE=0 turns it off.
"""
from __future__ import annotations

import os


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_CONTRACT_ON_WRITE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def check(st, files, cwd):
    if not _on() or not hasattr(st, "__dict__"):
        return
    goal = str(getattr(st, "goal", "") or "")
    if not goal:
        return
    try:
        from aiforge_core.runtime import git_target, typed_contract
        owners = {c[1] for c in typed_contract.claims(goal) if c[1]}
        if not owners:
            return
        stems = {os.path.splitext(os.path.basename(full))[0] for _shown, full in files}
        hit = owners & stems
        if not hit:
            return
        root = git_target.command_base(cwd) or cwd
        gaps = [g for g in typed_contract.missing(goal, root)
                if any(o in g for o in hit)]
    except Exception:  # noqa: BLE001 — a check never breaks a step
        return
    said = st.__dict__.setdefault("contract_on_write_said", set())
    key = tuple(sorted(gaps))
    if not gaps or key in said:
        return
    said.add(key)
    yield {"type": "thought", "role": "system",
           "text": f"📐 {', '.join(sorted(hit))} does not match what the request states "
                   f"({len(gaps)})"}
    from ._notes import hold
    hold(st, "[automated contract check — not the user] The request states how this code "
             "will be called, and the class you just wrote does not match it yet:\n- "
             + "\n- ".join(gaps[:6])
             + "\nDeclare exactly these (keep framework wiring as an extra, not instead).")
