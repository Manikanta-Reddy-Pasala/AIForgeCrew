"""Native OpenAI tool-calling for the simple chat loop.

The text ACTION/ARGS_JSON protocol makes local models emit ``ARGS_JSON: {}``
(arg-less tool calls). Native function-calling — what OpenWebUI uses on the same
endpoints — returns real structured arguments. This module decides WHEN to use
native (per-model capability probe + env override) and adapts a native reply
back into the exact text step the existing loop already parses — so the whole
loop (dispatch, edit-guard, verify, compaction) is reused unchanged; native FC
only changes HOW the next step is produced. Text protocol stays the fallback.
"""
from __future__ import annotations

import json
import re
import logging
import os

log = logging.getLogger("aiforge.chat.native")

# model id -> native-tool support (probed once, then cached).
_NATIVE_CACHE: dict[str, bool] = {}


def _protocol_setting() -> str:
    """``native`` (DEFAULT) | ``text`` | ``auto``. Native is the default
    everywhere — the text ACTION/ARGS_JSON protocol is the fumble-prone legacy
    path. ``auto`` probes the model once; ``text`` forces the legacy protocol.
    Native self-heals to text at runtime only on a DEFINITIVE tools-rejection
    (see :func:`make_native_complete_fn`), so defaulting native is safe even for
    an occasional model that can't do tools."""
    v = (os.environ.get("AIFORGE_CHAT_TOOL_PROTOCOL", "native") or "native").strip().lower()
    return v if v in ("native", "text", "auto") else "native"


def reset_native_cache() -> None:
    _NATIVE_CACHE.clear()


def _probe_timeout() -> int:
    try:
        return int(os.environ.get("AIFORGE_CHAT_NATIVE_PROBE_TIMEOUT_S", "12"))
    except (TypeError, ValueError):
        return 12


def _model_for(role: str) -> str:
    try:
        from aiforge_core.llm.router import resolve
        return resolve(role).model or role
    except Exception:  # noqa: BLE001
        return role


def _tools_unsupported(exc: Exception) -> bool:
    """A DEFINITIVE 'this endpoint can't do native tools' signal (vs a transient
    busy/timeout/transport error). Only a tools/function rejection counts — a
    timeout or connection drop on a model that's merely loading is inconclusive
    and must NOT permanently disable native."""
    # A 5xx / 429 is transient (busy / loading / rate-limited), NEVER a
    # definitive tools-rejection — check the code FIRST, before consuming the
    # one-shot body, so a transient error can't disable native.
    code = getattr(exc, "code", 0)
    if code == 429 or (isinstance(code, int) and code >= 500):
        return False
    # A tools-incapable OpenAI-compatible endpoint returns HTTP 400 whose REASON
    # is in the response BODY — `str(exc)` alone is just "HTTP Error 400: Bad
    # Request" (no tool/reject word). Classify on str(exc) + the HTTP body.
    try:
        from aiforge_core.llm.client._errors import _http_err_body
        body = _http_err_body(exc)
    except Exception:  # noqa: BLE001
        body = ""
    m = (str(exc) + " " + body).lower()
    mentions_tools = "tool" in m or "function" in m
    # DELIBERATELY excludes generic 400 words — 'invalid' (every OpenAI-compatible
    # 400 body carries `"type":"invalid_request_error"`), 'unknown' ('Unknown
    # parameter'), 'unrecognized'/'unexpected' (Jackson 'Unrecognized field') —
    # each of which, paired with a tools-schema echo (contains "function"), would
    # falsely + PERMANENTLY disable native. The tokens below name a real
    # tools-CAPABILITY rejection and do NOT appear in a generic 400.
    rejected = any(t in m for t in (
        "unsupported", "not support", "does not support", "no such tool",
        "no such function", "not allowed", "not implemented", "not capable",
        "tools are disabled", "function calling is disabled"))
    return mentions_tools and rejected


def _probe_native(role: str) -> bool:
    """Force one tiny tool call; the endpoint supports native FC iff it returns
    a ``tool_calls`` reply. Result is cached per model — but ONLY a definitive
    outcome (a real response, or a tools-rejection error). A transient failure
    (timeout / busy / model reloading) is inconclusive: it is NOT cached and we
    stay OPTIMISTIC (return True) so a momentary endpoint hiccup can't disable
    native for the whole process lifetime (it re-confirms on the next turn).
    Never raises."""
    model = _model_for(role)
    if model in _NATIVE_CACHE:
        return _NATIVE_CACHE[model]
    from aiforge_core.llm import client
    tools = [{"type": "function", "function": {
        "name": "aiforge_ping", "description": "Acknowledge readiness.",
        "parameters": {"type": "object",
                       "properties": {"ack": {"type": "string"}},
                       "required": []}}}]
    msgs = [{"role": "user", "content": "Call the aiforge_ping tool with ack='ok'."}]
    # tool_choice MUST be a string ("none"/"auto"/"required") — LM Studio (and
    # some other OpenAI-compatible servers) reject the object form with HTTP 400
    # ("Invalid tool_choice type: 'object'"). "required" forces a call so the
    # probe gets a deterministic positive signal on a tool-capable endpoint.
    try:
        m = client.complete_raw(
            role, msgs, tools=tools, tool_choice="required",
            max_tokens=64, timeout_s=_probe_timeout())
        ok = bool(m.get("tool_calls"))
        _NATIVE_CACHE[model] = ok          # definitive: endpoint responded
        return ok
    except Exception as exc:  # noqa: BLE001
        if _tools_unsupported(exc) and not _rejects_only_tool_choice(exc):
            _NATIVE_CACHE[model] = False   # definitive: endpoint rejects tools
            return False
        # A rejection that names ONLY tool_choice (the probe forces
        # tool_choice="required"; some servers do tools with "auto" but reject
        # the forced mode the RUNTIME never uses) is NOT a tools-capability
        # rejection — the model can do native FC. Stay optimistic, don't cache.
        # Also inconclusive (busy / timeout / reloading).
        log.info("native probe inconclusive (%s) — staying optimistic", exc)
        return True


def _rejects_only_tool_choice(exc: Exception) -> bool:
    """True when the rejection is specifically about the ``tool_choice`` PARAMETER
    (the forced-call mode the probe uses), not about tools support in general —
    so a model that does native FC with ``tool_choice="auto"`` isn't wrongly
    disabled just because it refuses ``"required"``."""
    try:
        from aiforge_core.llm.client._errors import _http_err_body
        m = (str(exc) + " " + _http_err_body(exc)).lower()
    except Exception:  # noqa: BLE001
        m = str(exc).lower()
    return "tool_choice" in m or "tool choice" in m


def native_tools_enabled(role: str) -> bool:
    """True when the simple loop should use native tool-calling for ``role``.
    ``native``/``text`` force it; ``auto`` (default) probes the model once."""
    s = _protocol_setting()
    if s == "text":
        return False
    # A model that hit a DEFINITIVE tools-rejection on a prior turn is cached
    # False — don't re-wire native (and don't emit the 'native active' banner
    # for a run that will silently run on text).
    if _NATIVE_CACHE.get(_model_for(role)) is False:
        return False
    if s == "native":
        return True
    return _probe_native(role)


# Sentinel: a tool_call whose arguments were ATTEMPTED but are unrecoverable
# (truncated/malformed JSON). Emitting ``ARGS_JSON: {}`` would silently drop the
# model's real args — the exact empty-args failure this feature kills — so we
# signal the caller to redo the turn on the hardened text path instead.
_NATIVE_ARGS_UNRECOVERABLE = "\x00native-args-unrecoverable"


def _resolve_call_args(raw):
    """Resolve a tool call's ``arguments``, DISTINGUISHING a legit empty-args
    call from a malformed one: dict → use it; None/""/"{}" → genuinely empty ({});
    a non-empty string that fails to parse → ATTEMPTED-but-broken (None, don't
    surrender to {})."""
    if isinstance(raw, dict):
        return raw
    if raw is None or (isinstance(raw, str) and raw.strip() in ("", "{}")):
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return None


def _action_text(name: str, args: dict) -> str:
    return f"ACTION: {name}\nARGS_JSON: {json.dumps(args, ensure_ascii=False)}"


def _synth_step(msg: dict) -> str:
    """Adapt a native assistant message into the text step the loop's ``_parse``
    already understands. A ``tool_calls`` reply → a synthetic ACTION/ARGS_JSON
    line carrying the REAL structured args; a plain reply → its recovered text
    (content, else the reasoning channel, think-stripped). Only the FIRST tool
    call becomes this step; :func:`_queued_steps` hands the loop the rest when
    they are all read-only. Returns the
    ``_NATIVE_ARGS_UNRECOVERABLE`` sentinel when a named call's arguments were
    attempted but can't be parsed (caller falls back to text for that turn)."""
    from aiforge_core.llm.client._text import _msg_text, _strip_think
    calls = msg.get("tool_calls") or []
    if not calls:
        # Out of tokens mid-reasoning: the reasoning channel is an unfinished
        # thought, not the reply — return nothing so the empty-turn retry runs.
        if (msg.get("_finish_reason") == "length"
                and not _strip_think((msg.get("content") or "").strip())):
            return ""
        return _msg_text(msg)
    fn = (calls[0] or {}).get("function") or {}
    name = fn.get("name") or ""
    if not name:
        return _msg_text(msg)          # nameless call carries no action → content
    args = _resolve_call_args(fn.get("arguments"))
    if not isinstance(args, dict):
        return _NATIVE_ARGS_UNRECOVERABLE
    from ._prompt import _COMPLETION_TOOL_NAMES
    if name.lower() in _COMPLETION_TOOL_NAMES:
        # A "done"-style pseudo-call: its answer is read off the step text, so
        # narration appended here would be published — minus its first line.
        return _action_text(name, args)
    return _action_text(name, args) + _narration(msg)


def _narration(msg: dict) -> str:
    """What the model SAID alongside its tool call, as a trailing THOUGHT line.

    The user watched that text stream in, and the step used to drop it — gone
    from the chat the moment the tool started, and never saved. It goes AFTER
    the action so its prose can never be read as the action or its args, and
    a line opening with "WORD:" is indented so it cannot end the thought early
    or pass for a protocol marker."""
    from aiforge_core.llm.client._text import _strip_think
    text = _strip_think((msg.get("content") or "").strip()).strip()
    # A hybrid model may also WRITE the text protocol in its content: keep the
    # prose, not a second copy of the call.
    text = re.sub(r"^[ \t]*THOUGHT[ \t]*:[ \t]*", "", text, flags=re.I)
    cut = re.search(r"^[ \t]*(ACTION|ARGS_JSON|FINAL|ASK)[ \t]*:", text, re.M)
    text = (text[:cut.start()] if cut else text).strip()
    if not text:
        return ""
    text = re.sub(r"(?m)^(?=[ \t]*[A-Za-z_]+[ \t]*:)", " ", text)
    return f"\nTHOUGHT: {text}"


def _batch_cap() -> int:
    """Most calls one reply may run without asking the model again."""
    try:
        return max(0, int(os.environ.get("AIFORGE_CHAT_BATCH_READS", "8")))
    except (TypeError, ValueError):
        return 8


#: Reads bounded by per-request timeouts of seconds. The turn deadline is only
#: checked between calls, so a read-only tool that waits for minutes by design
#: (a pipeline watch, a crawl, a type check, a document summary that calls a
#: model) never joins a batch.
#: The batchable reads that wait on a server or a subprocess: a batch (and an
#: ADK reply, see doer_tools._threaded) runs these at the same time. Each is
#: thread-safe: no chdir, no shared state beyond benign caches, and no writes
#: of ours but a TLS pin (net.trust, atomic and locked).
#: Local reads take milliseconds and run one after another.
CONCURRENT_READS = frozenset({
    "jira_read", "jira_search", "jira_transitions", "jira_worklog",
    "jira_remote_links",
    "confluence_read", "confluence_search", "confluence_children",
    "confluence_spaces", "confluence_page_by_title", "confluence_labels",
    "confluence_comments", "confluence_descendants",
    "gitlab_read", "gitlab_search", "gitlab_pipelines", "gitlab_pipeline",
    # A `codegraph` subprocess reading the SQLite index; the index is built
    # before the turn (ensure_indexed), never by a query.
    "codegraph_query", "codegraph_callers", "codegraph_callees",
    "codegraph_impact", "codegraph_explore",
    # Egress-gated inside the tool; the batch also checks the gate before
    # starting one early.
    "web_fetch",
})
BATCHABLE_READS = CONCURRENT_READS | {
    "file_read", "read_files", "read_lines", "list_dir", "find", "grep",
    "git_status", "git_diff", "git_log", "git_blame",
    "memory_lookup", "search_chat_sessions", "skill_search", "workflow_search",
    "resolve_repo", "list_services",
}
#: Bookkeeping the loop handles itself; safe to run inside a batch of reads.
_BATCHABLE = BATCHABLE_READS | {"plan_progress"}


def _queued_steps(msg: dict) -> "tuple[list[str], int]":
    """``(steps, skipped)`` for the 2nd..Nth tool calls of one reply, so a model
    that asks for five lookups at once gets them without four more round trips.

    Only when EVERY call is in :data:`BATCHABLE_READS`: a write depends on what the
    model saw before it, so a mixed batch keeps one call per turn (the first
    call runs; the model asks again). ``skipped`` counts the distinct calls that
    will not run — the loop tells the model. Each queued step still goes
    through every loop gate."""
    calls = msg.get("tool_calls") or []
    if len(calls) < 2:
        return [], 0
    first = _synth_step({**msg, "content": None})   # the call, not its narration
    batchable = True
    broken = 0
    wanted: list[str] = []
    for c in calls[1:]:
        fn = (c or {}).get("function") or {}
        name = fn.get("name") or ""
        batchable = batchable and name in _BATCHABLE
        args = _resolve_call_args(fn.get("arguments"))
        if not isinstance(args, dict):
            broken += 1
            continue
        step = _action_text(name, args)
        if step != first and step not in wanted:
            wanted.append(step)
    first_name = ((calls[0] or {}).get("function") or {}).get("name") or ""
    if not batchable or first_name not in _BATCHABLE:
        return [], len(wanted) + broken
    steps = wanted[:max(0, _batch_cap() - 1)]
    return steps, len(wanted) - len(steps) + broken


def _native_error_is_permanent(exc, model: str) -> bool:
    """DEFINITIVE: this model can't do native tools — cache + fall back to text
    for this and every future turn. A rejection that names ONLY tool_choice is
    NOT a tools-capability signal (same guard the probe uses)."""
    if _tools_unsupported(exc) and not _rejects_only_tool_choice(exc):
        _NATIVE_CACHE[model] = False
        log.info("native unsupported at runtime → text fallback (%s)", model)
        return True
    return False


def _native_error_transient(exc) -> bool:
    """A clearly TRANSIENT error (5xx/429/timeout/model-reloading) → let the
    loop's retry re-issue native. Anything else (an unclassified 400 — a strict
    server rejecting the 'tools' field with unfamiliar wording) is non-transient
    and falls back to text for THIS turn only."""
    try:
        from aiforge_core.llm.client._errors import _is_transient_exc
        return bool(_is_transient_exc(exc)[0])
    except Exception:  # noqa: BLE001
        return False


def _log_native_step(calls: list) -> None:
    """Observability: log whether a native step produced a tool_call or plain
    content so a run can be audited ("all calls native")."""
    if calls:
        fn = (calls[0] or {}).get("function") or {}
        log.info("native tool_call: %s (n=%d)", fn.get("name"), len(calls))
    else:
        log.info("native content step (no tool_call)")


# A user-role message the loop wrote, not the person. The body after the
# header (pytest's docs URL, a path named ticket) must not add tools either.
# A steer merged on with a blank line is the person's words and is kept.
_HARNESS_SEGMENT = re.compile(
    r"^(?:OBSERVATION:|\[loop guard|\[(?:[^\]]*not the user|system reminder)"
    r"[^\]]*\]|You (?:narrated|signalled|described) )")
# A steer, or the correction typed when a tool call is rejected. Both are
# the person's words and are merged onto the observation with a blank line.
_USER_SEGMENT = (
    "[NEW MESSAGE FROM THE USER",
    "The user rejected the last action",
)


# Blocks the server appends. They quote the tool catalog, READMEs and
# memory, so "jira" / "https://" in there would add every integration to
# the native list. The person's own words are the part before the marker.
_CUE_TAILS = (
    "\n\n---\n[Interpreted request",
    "\n\n---\n[Deliverable",
    "\n\n---\n[RESUME]",
)


def _cue_body(content: str) -> str:
    text = content or ""
    for mark in _CUE_TAILS:
        text = text.split(mark)[0]
    return text


def _convo_text(convo) -> str:
    """The user's own words, including a mid-run steer.

    Tool results, loop-guard notes, and automated checks are stored as user
    messages. A URL or the word ticket inside one of those must not add web
    or Jira tools. Once a harness header starts, the rest of that message is
    its body, until a steer or a rejection correction. The system prompt
    names every integration; it is not the user asking for those tools, and
    neither is a copy of that prompt stored as a user turn."""
    parts = []
    for message in convo or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "\n\n".join(
                str(part.get("text") or "") for part in content
                if isinstance(part, dict))
        content = _cue_body(str(content))
        if content.lstrip().startswith("You are AIForge"):
            continue
        dropping = False
        for segment in content.split("\n\n"):
            stripped = segment.lstrip()
            if not stripped:
                continue
            if stripped.startswith(_USER_SEGMENT):
                dropping = False
                parts.append(segment)
                continue
            if dropping or _HARNESS_SEGMENT.match(stripped):
                dropping = True
                continue
            parts.append(segment)
    return "\n".join(parts)


def select_native_tools(convo, *, mode: str = "act", builder: str = "",
                        schemas: list | None = None) -> list:
    """The native schemas this turn actually sends.

    The banner and the model call both use this, so the "(N tools)" line is
    the list on the wire. Family tools are added from the user's words, not
    from the system prompt (that prompt names every integration and used to
    make the count the whole gated catalog, about 63)."""
    from ._tools._schemas import NATIVE_TOOL_SCHEMAS, filter_native
    if schemas is None:
        try:
            from ._catalog_gate import gate_schemas
            schemas = gate_schemas(NATIVE_TOOL_SCHEMAS)
        except Exception:  # noqa: BLE001 — never break a turn
            schemas = list(NATIVE_TOOL_SCHEMAS)
    return filter_native(list(schemas), mode=mode or "act",
                         text=_convo_text(convo), builder=builder or "")


def make_native_complete_fn(mode: str = "act", builder: str = ""):
    """A drop-in ``complete_fn(role, convo) -> str`` that calls the model with
    native tools and returns the adapted text step. The core tool schemas go
    natively; the long tail stays in the system prompt and is still callable
    as a text ACTION, and is added natively when the message names that
    system. A steer can add tools. It does not remove them."""
    from aiforge_core.llm import client
    from ._tools._schemas import NATIVE_TOOL_SCHEMAS

    queued: list[str] = []
    skipped = [0]
    gated: list[dict] = []    # integration gate, once per turn
    tools: list[dict] = []    # grows if a later message names another system

    def take_queued() -> "tuple[list[str], int]":
        """The read-only calls the last reply batched after its first one, and
        how many of its other calls will not run."""
        items, n = list(queued), skipped[0]
        queued.clear()
        skipped[0] = 0
        return items, n

    def _fn(role: str, convo: list[dict]) -> str:
        take_queued()
        # Known-incapable model (a prior turn hit a definitive tools-rejection) →
        # text protocol, transparently. This is the ONLY thing that disables
        # native, and it's per-model + self-discovered, never transient.
        model = _model_for(role)
        if _NATIVE_CACHE.get(model) is False:
            return client.complete(role, convo)
        if not gated:
            try:
                from ._catalog_gate import gate_schemas
                gated[:] = gate_schemas(NATIVE_TOOL_SCHEMAS)
            except Exception as exc:  # noqa: BLE001 — never break a turn
                log.debug("schema gate failed, sending all: %s", exc)
                gated[:] = list(NATIVE_TOOL_SCHEMAS)
        have = {((s.get("function") or {}).get("name")) for s in tools}
        for schema in select_native_tools(
                convo, mode=mode, builder=builder, schemas=gated):
            name = (schema.get("function") or {}).get("name")
            if name not in have:
                tools.append(schema)
        try:
            msg = client.complete_raw(
                role, convo, tools=tools, tool_choice="auto")
        except Exception as exc:  # noqa: BLE001
            if _native_error_is_permanent(exc, model):
                return client.complete(role, convo)
            if _native_error_transient(exc):
                raise
            log.info("native call failed non-transiently → text this turn (%s)", exc)
            return client.complete(role, convo)
        _log_native_step(msg.get("tool_calls") or [])
        step = _synth_step(msg)
        if step == _NATIVE_ARGS_UNRECOVERABLE:
            # the model attempted tool args but they were truncated/malformed —
            # redo this turn on the hardened text path rather than emit empty args
            log.info("native args unrecoverable → text fallback for this turn")
            return client.complete(role, convo)
        queued[:], skipped[0] = _queued_steps(msg)
        return step

    _fn.take_queued = take_queued
    return _fn
