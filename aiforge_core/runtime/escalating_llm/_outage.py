"""EscalatingLlm and a model OUTAGE: every candidate failed because the model
is down — wait for it (llm/model_wait) and send the same request again,
instead of ending the team run or the ticket."""
from __future__ import annotations


def note(state: dict, exc: BaseException) -> None:
    """Remember the first candidate failure that was a model OUTAGE, so a later
    candidate failing for another reason (a cloud entry with no key) does not
    turn a wait into a failed run."""
    if state.get("outage") is not None:
        return
    try:
        from aiforge_core.llm import model_outage
        if model_outage.is_outage(exc):
            state["outage"] = exc
    except Exception:  # noqa: BLE001 — classification never breaks a call
        pass


def waiter_for(model, role: str):
    """A model_wait.Waiter aimed at ``model``'s endpoint (the primary)."""
    from aiforge_core.llm import model_wait

    from ._policy import _api_base_of
    from ._wrapper import _api_key_of
    return model_wait.Waiter(
        _api_base_of(model) if model is not None else "",
        api_key=_api_key_of(model) if model is not None else "",
        model=str(getattr(model, "model", "") or ""), what=role)
