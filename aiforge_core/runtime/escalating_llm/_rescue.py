"""How EscalatingLlm rescues a failed call: substituting a model the box
serves, or reloading the local model."""
from __future__ import annotations

import time as _time

from google.adk.models.llm_response import LlmResponse

from ._policy import (
    _is_transient_llm_error,
    _looks_like_missing_model,
)
from ._quieting import log


def _pkg():
    """``_wrapper``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``_wrapper``; patch any other
    name on this module."""
    import aiforge_core.runtime.escalating_llm._wrapper as package
    return package


class _RescueMixin:
    """Substitution and reload rescues for :class:`EscalatingLlm`."""

    @staticmethod
    def _substitution_allowed(exc) -> bool:
        """Whether a stand-in is warranted at all.

        Trigger differs by source: a MISSING model is a config error and every
        candidate is worth trying, while a model that is served but not
        answering only justifies the registry chain.

        The operator's kill switch applies HERE too. The direct-client rescue is
        gated on it and documents why: someone comparing models wants a wrong id
        to be a hard failure. Honouring it in chat and ignoring it in team mode
        is the same silent substitution the flag exists to prevent, on the path
        that runs a whole ticket.
        """
        if not (_looks_like_missing_model(exc) or _is_transient_llm_error(exc)):
            return False
        try:
            from aiforge_core.llm.client import _autofallback_enabled
            return bool(_autofallback_enabled())
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    def _registry_substitute(mid: str, base: str) -> str:
        """The operator's next configured model ON THIS ENDPOINT, or "".

        The same chain the chat path walks, so "I added four models, use the
        others when one dies" means the same thing in team mode. Registry rows
        that name ANOTHER host are for the text path, which can rebuild the
        endpoint; here the request is bound to this agent's own client, so only
        a different model on this endpoint is usable.
        """
        try:
            from aiforge_core.config import model_registry as _mr
            rows = _mr.chain_after(mid, base)
        except Exception:  # noqa: BLE001 — the registry is optional
            return ""
        want = (base or "").rstrip("/")
        for row in rows:
            if not (isinstance(row, dict) and str(row.get("model") or "").strip()):
                continue
            url = str(row.get("base_url") or "").strip().rstrip("/")
            if url and url != want:
                continue
            return str(row["model"]).strip()
        return ""

    @staticmethod
    def _served_substitute(model, mid: str, base: str) -> str | None:
        """A model this endpoint reports as loaded — the only option when the
        failure is "that model is not loaded here". None means the probe itself
        failed, and a rescue must never add a failure.

        Probed WITH the key: /v1/models is authenticated on most hosted
        endpoints, and an unauthenticated probe 401s, returns "no answer", and
        the rescue silently never fires — team mode dying on the exact config
        line chat recovers from.
        """
        try:
            from aiforge_core.llm.client._models import model_is_missing, pick_substitute
            served = model_is_missing(base, mid, _pkg()._api_key_of(model))
            return pick_substitute(mid, served or [])
        except Exception:  # noqa: BLE001
            return None

    def _substitute_id(self, exc, model) -> str:
        """The model id to stand in with, or "" for none. Registry first, then
        (for a missing model only) whatever the endpoint says it serves."""
        base = _pkg()._api_base_of(model)
        if not base:
            return ""
        mid = getattr(model, "model", "") or ""
        sub = self._registry_substitute(mid, base)
        if sub or not _looks_like_missing_model(exc):
            return sub
        return self._served_substitute(model, mid, base) or ""

    async def _substitute_model(self, exc, model, req, label, meta: dict):
        """Re-issue ONE attempt against a model this endpoint actually serves.

        Yields the responses when the stand-in worked and nothing when it did
        not — the caller then falls through to the cloud chain exactly as
        before. LiteLlm picks ``llm_request.model`` before its own, so the
        substitution is a stamped request, not a rebuilt model object.
        """
        pkg = _pkg()
        if not self._substitution_allowed(exc):
            return
        sub = self._substitute_id(exc, model)
        if not sub:
            return
        log.warning(
            "llm.model_substituted role=%s attempt=%s configured=%s using=%s "
            "api_base=%s — the configured model is not served here; fix the "
            "role config or load it", self.role, label,
            getattr(model, "model", "?"), sub, pkg._api_base_of(model))
        # PER-CALL, via the caller's dict. Stored on the instance it would be
        # cross-attributed the moment two calls share this EscalatingLlm (it is
        # built once per role per ticket), billing one call's tokens to the
        # other's model.
        meta["model"] = sub
        _tok = None
        try:
            await pkg._throttle_global(self.role)
            _tok = pkg._meter_record(self.role, sub)
            meta["token"] = _tok
            out = []
            async for r in model.generate_content_async(
                    req.model_copy(update={"model": sub}), stream=False):
                out.append(r)
        except Exception as sub_exc:  # noqa: BLE001
            pkg._meter_fail(_tok, sub_exc)
            log.warning("llm.model_substitute_failed role=%s err=%.200s",
                        self.role, str(sub_exc))
            return
        if not out or all(pkg._is_empty(r) for r in out):
            pkg._meter_fail(_tok, reason="empty")
            return
        for r in out:
            yield r

    async def _rescue_by_substitution(self, exc, model, req, label, t0):
        """Yield a stand-in model's answer, or nothing.

        PRIMARY only, like the LM-crash recovery and like the client-side
        rescue, which only ever substitutes the primary. A cloud candidate's 404
        for a decommissioned id must not be silently re-issued against whatever
        a proxy happens to serve: that is a billed generation on a model nobody
        chose.
        """
        if label not in ("primary", "primary_retry"):
            return
        meta: dict = {}
        out: list = []
        async for r in self._substitute_model(exc, model, req, label, meta):
            out.append(r)
        if not out:
            return
        # The stand-in produced the answer, so the accounting names IT: tokens,
        # budget and the Langfuse trace all used to short-circuit here, leaving
        # a rescued team run counted as a request with zero tokens and traced
        # against the model that generated nothing.
        used = meta.get("model")
        self._record_spend(used or label, out, meta.get("token"))
        # A rescue that worked clears the demotion the failure would otherwise
        # leave behind — else every later call re-walks the whole cloud chain
        # before reaching the same rescue.
        self.primary_demoted = False
        _pkg()._mirror_to_langfuse(self.role, req, out,
                            used or getattr(model, "model", "") or label,
                            int((_time.monotonic() - t0) * 1000))
        for r in out:
            yield r

    def _should_try_lm_reload(self, label: str, err_str: str) -> bool:
        """LM Studio MLX crash mid-pipeline ("model has crashed" / "No models
        loaded"). Without the reload, sticky-demotion locks us off the local
        primary for the rest of the ticket and a stress run starves on cloud
        rate limits. One attempt per pipeline run — a flapping LM Studio would
        otherwise trigger an SSH-load storm."""
        if label not in ("primary", "primary_retry") or self.lm_recovery_tried:
            return False
        from .. import local_starter
        return bool(local_starter.looks_like_lm_crash(err_str))

    async def _rescue_by_lm_reload(self, model, req, label, target, t0,
                                   out: dict):
        """Force-reload the crashed local model and retry the SAME attempt once.
        Yields its answer, or nothing. ``out["exc"]`` carries a retry failure
        back to the caller so the chain reports the freshest error."""
        pkg = _pkg()
        from .. import local_starter
        self.lm_recovery_tried = True
        recovered = local_starter.try_recover(
            pkg._api_base_of(model), getattr(model, "model", None) or None)
        log.warning("llm.lm_crash_recovery role=%s recovered=%s",
                    self.role, recovered)
        if not recovered:
            return
        buffered: list[LlmResponse] = []
        tok = None
        try:
            # Gated like every other send: a recovery retry is a real request to
            # the model and must be both throttled and counted.
            await pkg._throttle_global(self.role)
            tok = pkg._meter_record(self.role, target)
            async for r in model.generate_content_async(req, stream=False):
                buffered.append(r)
        except Exception as exc:  # noqa: BLE001
            pkg._meter_fail(tok, exc)
            out["exc"] = exc
            log.warning("llm.recovery_retry_failed role=%s err=%s",
                        self.role, str(exc)[:200])
            return
        if not buffered or all(pkg._is_empty(r) for r in buffered):
            # Counted, answered nothing — the same failure the `attempt_empty`
            # branch records for the normal path.
            pkg._meter_fail(tok, reason="empty")
            return
        # A recovered response is a real one: count what it WROTE and record its
        # spend. This branch yielded and returned before ever reaching the
        # accounting block, so a crash-and-recover box reported traffic with no
        # tokens behind it.
        self._record_spend(target or label, buffered, tok)
        log.info("llm.recovered role=%s after_lm_reload", self.role)
        pkg._mirror_to_langfuse(self.role, req, buffered,
                            getattr(model, "model", "") or label,
                            int((_time.monotonic() - t0) * 1000))
        for r in buffered:
            yield r

    async def _rescue_after_failure(self, exc, model, req, label, target, t0,
                                    state: dict):
        """Both rescue paths for a failed attempt, in order: a stand-in model,
        then an LM-Studio reload. Yields a rescued answer, or nothing — in which
        case the caller moves on to the next candidate."""
        state["exc"] = exc
        async for r in self._rescue_by_substitution(exc, model, req, label, t0):
            state["done"] = True
            yield r
        if state["done"]:
            return
        err_str = str(exc)
        log.warning(
            "llm.attempt_failed role=%s attempt=%s model=%s api_base=%s "
            "errtype=%s err=%s", self.role, label,
            getattr(model, "model", "?"), _pkg()._api_base_of(model) or "?",
            type(exc).__name__, err_str[:800])
        if self._should_try_lm_reload(label, err_str):
            out: dict = {}
            async for r in self._rescue_by_lm_reload(model, req, label,
                                                     target, t0, out):
                state["done"] = True
                yield r
            if not state["done"] and "exc" in out:
                # The chain reports the freshest error, so a recovery retry that
                # died replaces the crash that triggered it.
                state["exc"] = out["exc"]
        if not state["done"] and label == "primary":
            self._record_primary_failure()
