from __future__ import annotations

import os


# Below this resolved context window (tokens) a small local box gets lean
# ("cave") context automatically — the operator needn't flip a setting.
# Override the threshold with AIFORGE_CAVE_AUTO_WINDOW; a big-window model
# stays above it and keeps the full context.
_CAVE_AUTO_WINDOW_DEFAULT = 49152   # 48K


def _cave_auto_window() -> int:
    raw = os.environ.get("AIFORGE_CAVE_AUTO_WINDOW")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _CAVE_AUTO_WINDOW_DEFAULT


def _resolved_window(role: str | None = None) -> int:
    """The resolved context window in tokens. Routes through the ONE window
    source (``model_registry.effective_context_window``) so cave sizing, the
    seed/sys-prompt budgets and the window-scaled caps all agree (A3): prefer
    the per-role registry / auto-detected window, else the global setting."""
    try:
        from aiforge_core.config import model_registry
        return int(model_registry.effective_context_window(role))
    except Exception:  # noqa: BLE001
        try:
            from aiforge_core.config import runtime_settings
            return int(runtime_settings.get("context_window"))
        except Exception:  # noqa: BLE001
            return 131072


def _window_scaled(floor: int, frac: float, role: str | None = None) -> int:
    """A window-relative section cap: ``max(floor, window_chars × frac)``.

    The floor is today's fixed value (so a 32K window is byte-identical); on a
    bigger window the cap grows with it so a 256K box is actually used. Uses the
    SAME resolved-window source as every other budget (A3). Soft-fails to the
    floor on any error."""
    try:
        win = _resolved_window(role)
        return max(floor, int(win * 4 * frac))
    except Exception:  # noqa: BLE001
        return floor


def _cave_mode() -> bool:
    """Cave mode = leanest useful context (smaller repo map, skip optional
    skills/workflows/mentions blocks, fewer memory hits, tighter condense
    budget).

    Resolution — an EXPLICIT operator choice always wins, in order:
      1. env ``AIFORGE_CAVE_MODE`` (1/0 force on/off)
      2. an explicitly-stored ``cave_mode`` setting (UI wrote it — 1/0)
    Only when NEITHER is set do we AUTO-enable cave for a small resolved
    context window (<= ``AIFORGE_CAVE_AUTO_WINDOW``, default 48K), so a
    small local box gets lean context without the operator flipping a
    setting while a big-window model keeps the full context."""
    env = os.environ.get("AIFORGE_CAVE_MODE")
    if env is not None:
        return env not in ("0", "false", "")
    try:
        from aiforge_core.config import runtime_settings
        # Distinguish an explicitly-stored value from the unset default (a
        # stored 0 = operator opted OUT and must be respected).
        stored = runtime_settings._read_store().get("cave_mode")
        if isinstance(stored, int):
            return stored > 0
    except Exception:  # noqa: BLE001
        pass
    # Unset → auto-enable when the window is small enough that the full
    # context wouldn't fit comfortably.
    try:
        return _resolved_window() <= _cave_auto_window()
    except Exception:  # noqa: BLE001
        return False


def _compress_prompt(text: str) -> str:
    """Squeeze whitespace bloat out of the assembled prompt before it hits the
    LLM — dense context fits a small local window better and costs fewer tokens
    (the user's 'caveman'-style ask). SAFE/structural only: collapses runs of
    blank lines to one, strips trailing spaces, and drops consecutive duplicate
    lines. No words removed, no reordering — semantics unchanged. Off with
    AIFORGE_CHAT_COMPRESS_PROMPT=0."""
    if os.environ.get("AIFORGE_CHAT_COMPRESS_PROMPT", "1") in ("0", "false"):
        return text
    out: list[str] = []
    blanks = 0
    prev = None
    for raw in text.splitlines():
        ln = raw.rstrip()
        if not ln:
            blanks += 1
            if blanks > 1:
                continue          # collapse multiple blank lines to one
            out.append("")
            continue
        blanks = 0
        if ln == prev:
            continue              # drop an immediately-repeated line
        out.append(ln)
        prev = ln
    return "\n".join(out).strip()


# Measured size of the built system prompt (``_SYSTEM``) in chars — reserved
# out of the window so it isn't counted as available history.
_SYSTEM_PROMPT_CHARS = 14000
# Never let the history budget collapse to <=0 on a tiny window.
_CTX_BUDGET_FLOOR_CHARS = 4000


def _ctx_budget_chars(role: str | None = None,
                      sys_chars: int | None = None) -> int:
    """Char budget for the running conversation before auto-condensing. 0
    disables. Explicit override: AIFORGE_CHAT_CONTEXT_BUDGET_CHARS. Otherwise
    SIZED TO THE CONFIGURED MODEL WINDOW (context_window tokens → ~4 chars/token)
    MINUS the reservations that aren't available for history — the output cap
    (``max_output_tokens``) and the system prompt — so on a 32K local window the
    budget leaves real room for INPUT instead of assuming the whole window is
    history. ``sys_chars`` reserves the ACTUAL assembled system-prompt size when
    the caller knows it (M1); when omitted it falls back to the ~14K
    ``_SYSTEM_PROMPT_CHARS`` estimate. A cave/non-cave headroom fraction is then
    applied to the remaining usable space, and a floor keeps the budget positive
    on a tiny window."""
    env = os.environ.get("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    reserve_sys = _SYSTEM_PROMPT_CHARS if sys_chars is None else max(0, int(sys_chars))
    win = 0
    # Per-model context window (registry) for this role wins over the global.
    if role:
        try:
            from aiforge_core.config import model_registry
            win = int(model_registry.context_window_for_role(role))
        except Exception:  # noqa: BLE001
            win = 0
    if win <= 0:
        try:
            from aiforge_core.config import runtime_settings
            win = int(runtime_settings.get("context_window"))
        except Exception:  # noqa: BLE001
            win = 0
    # Fraction of the (post-reserve) window kept as live history before we
    # condense. This is a SAFETY margin ON TOP of the explicit output + system
    # reservations below (the 4-chars/token estimate is imprecise and a turn's
    # tool output can still grow mid-flight), so it is < 1.0 on purpose — but
    # the old 0.55 was too conservative and made a big window (e.g. a 256k model)
    # feel like ~120k. 0.85 uses most of a large window (256k → condense ~210k)
    # while still leaving a ~15% cushion for the 4-chars/token estimate error +
    # mid-turn tool growth. Cave mode still condenses sooner. Tunable per deploy.
    _default_frac = 0.40 if _cave_mode() else 0.85
    try:
        headroom = float(os.environ.get("AIFORGE_CTX_HISTORY_FRACTION", _default_frac))
        headroom = min(0.95, max(0.15, headroom))  # clamp to a sane band
    except (TypeError, ValueError):
        headroom = _default_frac
    if win > 0:
        # Reserve what the request needs beyond history: the model's own reply
        # (output cap) and the system prompt. ~4 chars/token.
        try:
            from aiforge_core.config import runtime_settings
            out_chars = int(runtime_settings.get("max_output_tokens")) * 4
        except Exception:  # noqa: BLE001
            out_chars = 4096 * 4
        usable = win * 4 - out_chars - reserve_sys
        budget = int(max(usable, _CTX_BUDGET_FLOOR_CHARS) * headroom)
        return max(budget, _CTX_BUDGET_FLOOR_CHARS)
    return 24000 if _cave_mode() else 48000


# ── system-prompt budgeting (Fix C2) ────────────────────────────────────
# convo[0] (the system message) is NEVER shrunk by _compact_convo (it only
# condenses convo[1:-keep_recent]). Every dynamic block (repo summary/map,
# skills, workflows, mentions, memory + chat recall, images) is appended to
# it, so on a small window the un-condensable system prompt alone can
# overflow. We cap the assembled system prompt to a fraction of the window,
# dropping/truncating the LOWEST-priority injected blocks first and always
# keeping the core prompt + rules.
_SYS_PROMPT_FLOOR_CHARS = 8000


def _sys_prompt_frac() -> float:
    """Fraction of the window reserved for the (un-condensable) system prompt.
    Default 0.35 (env ``AIFORGE_SYS_PROMPT_FRAC``) — co-budgeted with the Doer
    seed (also 0.35) + the output cap so seed+sysprompt+output ≤ window (A1)."""
    try:
        return float(os.environ.get("AIFORGE_SYS_PROMPT_FRAC", "0.35"))
    except (TypeError, ValueError):
        return 0.35


def _sys_prompt_budget_chars(role: str | None = None) -> int:
    """Char cap for the assembled system prompt = :func:`_sys_prompt_frac` of
    the resolved window in chars, floored (scaled down on small windows).
    Threads ``role`` so it uses the SAME per-role window as the seed + history
    budgets (A3).

    C1: co-budgeted with the Doer seed + the output reservation so
    ``seed + sysprompt + out ≤ window×4`` holds even at 4K/8K/16K, where the
    fixed 8000 floor + a full-window output reservation used to overflow. The
    output reservation is capped at a window fraction and the floor scales
    down with whatever is left."""
    try:
        win = _resolved_window(role)
    except Exception:  # noqa: BLE001
        win = 32768
    win_chars = win * 4
    sys_frac = _sys_prompt_frac()
    try:
        from aiforge_core.runtime import text_doer as _td
        from aiforge_core.config import runtime_settings
        out_tok_chars = int(runtime_settings.get("max_output_tokens")) * 4
        out_chars = _td._out_reserve_chars(win_chars, out_tok_chars)
    except Exception:  # noqa: BLE001
        out_chars = min(8192 * 4, int(win_chars * 0.4))
    sys_reserve = int(win_chars * sys_frac)
    usable = win_chars - out_chars - sys_reserve
    floor = min(_SYS_PROMPT_FLOOR_CHARS, max(0, usable) // 3)
    return max(sys_reserve, floor)


_SYS_CAP_MARK = "\n…(system prompt truncated to fit context window)\n"


def _cap_system_prompt(sys_msg: str, budget: int, *, protect: int = 0) -> str:
    """Guarantee ``len(sys_msg) <= budget`` — the backstop under the block-aware
    assembly. Preserves the first ``protect`` chars (the core prompt + rules)
    and truncates the lower-priority injected TAIL first; if even the core
    exceeds the budget it hard-truncates. No-op when already under cap or
    ``budget <= 0``. Soft: never raises."""
    try:
        if budget <= 0 or len(sys_msg) <= budget:
            return sys_msg
        # Keep the first `budget - marker` chars (the core prompt + rules sit at
        # the FRONT, so the front-preserving cut drops the injected tail first).
        keep = budget - len(_SYS_CAP_MARK)
        if keep <= 0:
            return sys_msg[:max(0, budget)]
        return sys_msg[:keep] + _SYS_CAP_MARK
    except Exception:  # noqa: BLE001
        return sys_msg


def _ctx_on(block: str) -> bool:
    """Is the dynamic-context ``block`` injected this turn? Operator knob —
    ``ctx_no_{block}`` (runtime setting / env) = 1 turns it off. Default ON.
    Blocks: recall · mentions · skills · workflows · repomap · summary."""
    try:
        from aiforge_core.config import runtime_settings
        return int(runtime_settings.get(f"ctx_no_{block}")) == 0
    except Exception:  # noqa: BLE001
        return True
