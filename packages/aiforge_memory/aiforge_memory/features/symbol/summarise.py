"""Per-symbol LLM summarisation — Stage 6.5.

For non-trivial methods/functions in a repo, ask the LLM for a 1-line
behavior description (what it DOES, not what types it has). Result lands
on ``Symbol_v2.summary``.

Filters (tunable via env):
- kind in {method, function}                  AIFORGE_SYMSUM_KINDS
- line_count >= min_lines (default 8)         AIFORGE_SYMSUM_MIN_LINES
- file size <= MAX_FILE_BYTES                 AIFORGE_SYMSUM_MAX_FILE_BYTES
- skip body if no bytes between line_start
  and line_end
- skip getters / setters by signature shape

Soft contract:
- LLM bad JSON → keep prior summary, mark skipped_reason=llm_error
- LLM unreachable → skip silently, increment counter
- empty 'summary' from LLM → trivial method, leave Symbol_v2.summary unset
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from aiforge_memory.features.symbol.extract import WalkedFile

PROMPT_PATH = Path(__file__).parent / "prompts" / "symbol_summary.txt"
DEFAULT_LM_URL = os.environ.get(
    "AIFORGE_CODEMEM_LM_URL",
    os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
)
DEFAULT_MODEL = os.environ.get(
    "AIFORGE_CODEMEM_LM_MODEL", "qwen3.6-27b-instruct",
)
MIN_LINES = int(os.environ.get("AIFORGE_SYMSUM_MIN_LINES", "8"))
MAX_FILE_BYTES = int(os.environ.get(
    "AIFORGE_SYMSUM_MAX_FILE_BYTES", "262144",
))
KINDS_RAW = os.environ.get("AIFORGE_SYMSUM_KINDS", "method,function")
ALLOWED_KINDS = {k.strip().lower() for k in KINDS_RAW.split(",") if k.strip()}

# Cap body size sent to the LLM. mlx-lm 0.31 wedges on long prompts
# under sustained load; keep these conservative.
BODY_HEAD_LINES = int(os.environ.get("AIFORGE_SYMSUM_HEAD_LINES", "30"))
BODY_TAIL_LINES = int(os.environ.get("AIFORGE_SYMSUM_TAIL_LINES", "5"))
# Throttle: wait between successive LLM calls so mlx-lm has time to
# release internal state. 0.0 = no throttle.
INTER_CALL_DELAY_S = float(os.environ.get(
    "AIFORGE_SYMSUM_THROTTLE_S", "1.0",
))
# Per-request: timeout + retry. mlx-lm sometimes resets first SYN
# under load — one quick retry recovers most of those.
REQUEST_TIMEOUT_S = float(os.environ.get(
    "AIFORGE_SYMSUM_TIMEOUT_S", "120.0",
))
RETRY_MAX = int(os.environ.get("AIFORGE_SYMSUM_RETRY_MAX", "1"))
RETRY_BACKOFF_S = float(os.environ.get(
    "AIFORGE_SYMSUM_RETRY_BACKOFF_S", "3.0",
))
# Circuit breaker — if N consecutive calls fail, abort the whole run
# rather than burning 2000+ requests against a dead server.
ABORT_AFTER_CONSECUTIVE_ERRORS = int(os.environ.get(
    "AIFORGE_SYMSUM_ABORT_AFTER", "8",
))
# Concurrency — LM Studio's PARALLEL slot count. 1 keeps the legacy
# serial behaviour. >1 dispatches calls via a thread pool.
CONCURRENCY = max(1, int(os.environ.get(
    "AIFORGE_SYMSUM_CONCURRENCY", "1",
)))


class SymbolSummaryAborted(RuntimeError):
    """Raised when the LLM is failing too consistently to continue."""


@dataclass
class SymbolSummary:
    repo: str
    fqname: str
    summary: str = ""
    skipped_reason: str = ""    # "" | "kind" | "too_short" | "too_large" | "llm_error" | "trivial"


# Heuristic: the signature of a getter/setter rarely has more than a
# return statement or assignment in its body. We additionally require
# a meaningful line span, but keep the regex as a cheap pre-filter.
_GETTER_SIG = re.compile(r"\b(get|is|has|set)[A-Z]\w*\s*\(")


def _file_bytes(cache: dict, repo_root: Path, path: str) -> bytes | None:
    """Cached file contents, or None when unreadable or over the size cap."""
    buf = cache.get(path)
    if buf is not None:
        return buf
    try:
        buf = (repo_root / path).read_bytes()
    except OSError:
        return None
    if len(buf) > MAX_FILE_BYTES:
        return None
    cache[path] = buf
    return buf


def _file_candidates(wf: WalkedFile, floor: int) -> list:
    """``(wf, symbol, n_lines)`` for every symbol big enough to be worth a call."""
    out = []
    for sym in wf.symbols:
        if (getattr(sym, "kind", "") or "").lower() not in ALLOWED_KINDS:
            continue
        ls = getattr(sym, "line_start", 0) or 0
        le = getattr(sym, "line_end", 0) or 0
        n_lines = max(0, le - ls + 1)
        if n_lines < floor:
            continue
        # A getter/setter body is almost always a single return; only pay for
        # one that is longer than that.
        if n_lines < 3 and _GETTER_SIG.search(
                getattr(sym, "signature", "") or ""):
            continue
        out.append((wf, sym, n_lines))
    return out


def _gather_candidates(walked: list[WalkedFile], repo_root: Path,
                       floor: int) -> tuple[list, dict[str, bytes]]:
    """Everything worth summarising, plus the file bytes it was found in.

    The cache is returned rather than re-read per symbol: a 200-symbol file
    would otherwise be opened 200 times.
    """
    candidates: list = []
    cache: dict[str, bytes] = {}
    for wf in walked:
        if wf.parse_error or not wf.symbols:
            continue
        if _file_bytes(cache, repo_root, wf.path) is None:
            continue
        candidates += _file_candidates(wf, floor)
    return candidates, cache


def _notify(on_each, ss: SymbolSummary, idx: int, total: int) -> None:
    """Progress callback. The caller's own error must not kill the batch."""
    if on_each is None:
        return
    try:
        on_each(ss, idx, total)
    except Exception:
        pass


def _process_one(wf: WalkedFile, sym, *, repo: str,
                 file_bytes: bytes) -> SymbolSummary:
    """Single LLM round-trip for one candidate. Pure: no shared state."""
    ss = SymbolSummary(repo=repo, fqname=sym.fqname)
    body = _slice_body(file_bytes, sym.line_start, sym.line_end)
    if not body.strip():
        ss.skipped_reason = "too_short"
        return ss
    try:
        parsed = _parse(_call_llm(
            body=body, signature=sym.signature or "",
            doc=getattr(sym, "doc_first_line", "") or "",
            lang=wf.lang or "", path=wf.path, fqname=sym.fqname,
        ))
        if parsed is None:
            ss.skipped_reason = "llm_error"
        elif not parsed:
            ss.skipped_reason = "trivial"
        else:
            ss.summary = parsed
    except Exception:
        ss.skipped_reason = "llm_error"
    return ss


def _summarise_serial(candidates: list, cache: dict, *, repo: str,
                      on_each) -> list[SymbolSummary]:
    """The ``CONCURRENCY <= 1`` path — one call at a time, strict-consecutive
    circuit breaker, and the inter-call throttle mlx-lm needs."""
    out: list[SymbolSummary] = []
    total = len(candidates)
    consecutive_errors = 0
    for idx, (wf, sym, _) in enumerate(candidates):
        ss = _process_one(wf, sym, repo=repo, file_bytes=cache[wf.path])
        out.append(ss)
        consecutive_errors = (consecutive_errors + 1
                              if ss.skipped_reason == "llm_error" else 0)
        _notify(on_each, ss, idx + 1, total)
        if consecutive_errors >= ABORT_AFTER_CONSECUTIVE_ERRORS:
            raise SymbolSummaryAborted(
                f"{consecutive_errors} consecutive LLM errors — "
                "aborting; restart the LLM server and retry"
            )
        if INTER_CALL_DELAY_S > 0 and idx + 1 < total:
            time.sleep(INTER_CALL_DELAY_S)
    return out


class _ErrorWindow:
    """Rolling-window circuit breaker for the concurrent path.

    Strict-consecutive counting is order-sensitive, and order is not guaranteed
    once calls run in a pool — so this trips when the last ``size`` completions
    contain ABORT_AFTER_CONSECUTIVE_ERRORS or more failures.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self.done = 0
        self._lock = threading.Lock()
        self._recent: list[int] = []          # 1 = error, 0 = ok

    def record(self, ss: SymbolSummary) -> bool:
        """Record one completion. True while the batch should keep going."""
        with self._lock:
            self._recent.append(1 if ss.skipped_reason == "llm_error" else 0)
            if len(self._recent) > self.size:
                self._recent.pop(0)
            self.done += 1
            full = len(self._recent) >= self.size
            errors = sum(self._recent)
        return not (full and errors >= ABORT_AFTER_CONSECUTIVE_ERRORS)


def _result_or_error(fut, repo: str, sym) -> SymbolSummary:
    """A worker's result, or an llm_error summary when the worker itself blew up."""
    try:
        return fut.result()
    except Exception:
        return SymbolSummary(repo=repo, fqname=sym.fqname,
                             skipped_reason="llm_error")


def _summarise_concurrent(candidates: list, cache: dict, *, repo: str,
                          on_each) -> list[SymbolSummary]:
    """Pool sized to the LLM server's PARALLEL slot count.

    No throttle here — the queue itself bounds concurrency.
    """
    out: list[SymbolSummary] = []
    total = len(candidates)
    window = _ErrorWindow(max(8, ABORT_AFTER_CONSECUTIVE_ERRORS * 2))
    abort_msg: str | None = None
    with ThreadPoolExecutor(max_workers=CONCURRENCY,
                            thread_name_prefix="symsum") as pool:
        futures = {
            pool.submit(_process_one, wf, sym, repo=repo,
                        file_bytes=cache[wf.path]): sym
            for wf, sym, _ in candidates
        }
        for fut in as_completed(futures):
            ss = _result_or_error(fut, repo, futures[fut])
            out.append(ss)
            keep_going = window.record(ss)
            _notify(on_each, ss, window.done, total)
            if not keep_going:
                abort_msg = (
                    f"{ABORT_AFTER_CONSECUTIVE_ERRORS}+ errors in last "
                    f"{window.size} calls — aborting"
                )
                # Cancel pending futures so the pool drains fast.
                for f in futures:
                    if not f.done():
                        f.cancel()
                break
    if abort_msg:
        raise SymbolSummaryAborted(abort_msg)
    return out


def summarise_symbols(
    walked: list[WalkedFile],
    *,
    repo: str,
    repo_root: str | Path,
    limit: int | None = None,
    min_lines: int | None = None,
    on_each: callable | None = None,
) -> list[SymbolSummary]:
    """One LLM call per qualifying symbol. Order: largest body first
    so the most expensive things land in the budget.

    Args:
        limit: hard cap on LLM calls (None = unlimited)
        min_lines: override env MIN_LINES floor
        on_each: optional callback ``fn(summary: SymbolSummary, idx: int,
                 total: int) -> None`` invoked after each LLM response.
                 The CLI uses this to write incrementally + emit progress
                 instead of waiting for the whole batch.
    """
    repo_root = Path(repo_root)
    floor = MIN_LINES if min_lines is None else int(min_lines)
    candidates, cache = _gather_candidates(walked, repo_root, floor)

    # Largest first — gives the LLM budget the highest-value items first in
    # case `limit` cuts the tail.
    candidates.sort(key=lambda x: x[2], reverse=True)
    if limit is not None:
        candidates = candidates[:max(0, int(limit))]

    if CONCURRENCY <= 1:
        return _summarise_serial(candidates, cache, repo=repo, on_each=on_each)
    return _summarise_concurrent(candidates, cache, repo=repo, on_each=on_each)


def _slice_body(content: bytes, line_start: int, line_end: int) -> str:
    """Return UTF-8 slice for inclusive line range, head-tail truncated
    to keep prompts bounded. line_start/line_end are 1-based."""
    lines = content.decode("utf-8", errors="replace").splitlines()
    if line_start < 1:
        line_start = 1
    if line_end < line_start:
        line_end = line_start
    span = lines[line_start - 1: line_end]
    if len(span) > BODY_HEAD_LINES + BODY_TAIL_LINES:
        span = (
            span[:BODY_HEAD_LINES]
            + ["    // … truncated …"]
            + span[-BODY_TAIL_LINES:]
        )
    return "\n".join(span)


# Grouped so the precedence is visible: the anchors belong to their OWN
# branch (an opening fence at the start of a line, a closing fence at the end),
# which is what `^a|b$` already meant and what a reader could not see.
_FENCE_RE = re.compile(r"(?:^```(?:json)?\s*\n?)|(?:\n?```\s*$)", re.MULTILINE)
# Match the FIRST `{"summary":"..."}` JSON object anywhere in the text —
# necessary when the model wraps the answer in a thinking dump.
_SUMMARY_JSON_RE = re.compile(
    r'\{\s*"summary"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
    re.DOTALL,
)


def _parse(raw: str) -> str | None:
    """Return the summary string, '' for trivial, or None on error.

    Strategies, in order:
      1. fence-stripped JSON parse
      2. balanced-brace fallback (first { to last })
      3. regex extract — finds {"summary":"..."} embedded inside a
         thinking-mode preamble
    """
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return str(obj.get("summary", "")).strip()
    except json.JSONDecodeError:
        pass
    # Balanced-brace fallback
    i = cleaned.find("{")
    j = cleaned.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(cleaned[i : j + 1])
            if isinstance(obj, dict):
                return str(obj.get("summary", "")).strip()
        except json.JSONDecodeError:
            pass
    # Regex extract
    m = _SUMMARY_JSON_RE.search(cleaned)
    if m:
        # Unescape JSON string escapes
        try:
            return json.loads('"' + m.group(1) + '"')
        except json.JSONDecodeError:
            return m.group(1).strip()
    return None


def _summary_prompt(*, body: str, signature: str, doc: str,
                    lang: str, fqname: str) -> str:
    """The single user message we send.

    Compact "system" instructions inline; the file at PROMPT_PATH is the
    reference but here we keep the LLM-facing text tight to avoid token-length
    triggers in mlx-lm. The `/no_think` prefix is the Qwen3 convention for
    suppressing chain-of-thought at the prompt level — not every LM Studio
    build honours chat_template_kwargs.enable_thinking.
    """
    jwt_example = (
        '{"summary": "Generates a JWT for the given user, signing it '
        'with the configured HS256 key."}'
    )
    return (
        "/no_think\n"
        "Task: summarise the method below in ONE sentence (max 25 words, "
        "present tense). Focus on what it DOES — side effects, IO, "
        "control flow, error paths. Skip type paraphrase.\n"
        "\n"
        "Output: a single JSON object on one line, key 'summary', value "
        "is your sentence. If the method is trivial (pure getter/setter/"
        "delegate/DTO), the value MUST be the empty string.\n"
        "\n"
        "Example output for a JWT generator:\n"
        + jwt_example + "\n"
        "Example output for a getter:\n"
        '{"summary": ""}\n'
        "\n"
        "DO NOT echo this prompt. DO NOT output the literal three-dot "
        "placeholder.\n"
        "---\n"
        f"Symbol: {fqname}\n"
        f"Lang: {lang}\n"
        f"Signature: {signature}\n"
        + (f"Doc: {doc}\n" if doc else "")
        + f"Body:\n{body}\n"
    )


def _message_text(payload: dict) -> str:
    """The assistant's text from one chat-completions body.

    Some Qwen3 thinking models leave content="" and put the answer (which
    often still contains our JSON) in reasoning_content, so try content first.
    """
    choices = payload.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return ((msg.get("content") or "").strip()
            or (msg.get("reasoning_content") or "").strip())


def _post_summary(raw_body: bytes, headers: dict) -> str:
    """One request, one fresh client. Raises on any non-2xx."""
    import httpx

    url = DEFAULT_LM_URL.rstrip("/") + "/chat/completions"
    # http2=False, no client reuse, no transport pool — see _call_llm.
    with httpx.Client(
        timeout=REQUEST_TIMEOUT_S,
        http2=False,
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=1),
    ) as c:
        r = c.post(url, content=raw_body, headers=headers)
    r.raise_for_status()
    return _message_text(r.json())


def _call_llm(
    *, body: str, signature: str, doc: str,
    lang: str, path: str, fqname: str,
) -> str:
    """Real LLM call. Direct httpx + fresh client per call — OpenAI SDK
    keep-alive behaviour mlx-lm 0.31 doesn't tolerate. Multi-message
    chats also wedge mlx-lm 0.31, so we fold the system rules into
    one user message.

    Bounded retry on transient transport errors (RETRY_MAX backed by
    AIFORGE_SYMSUM_RETRY_MAX). An HTTP status error is not retried: the
    server answered, and asking again the same way gets the same answer.
    """
    # The prompt is built from the symbol itself; `path` rides along so every
    # caller can pass the same set of fields. Accepted, deliberately ignored.
    del path
    import httpx

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": _summary_prompt(
            body=body, signature=signature, doc=doc, lang=lang, fqname=fqname,
        )}],
        "temperature": 0.0,
        # Bumped from 120: thinking-mode Qwen3 needs headroom to either
        # emit the cot AND the JSON, or — when /no_think + enable_thinking
        # both fail — at least let the cot finish so we can pluck the JSON
        # from reasoning_content.
        "max_tokens": 1024,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    # Serialised once so every attempt sends byte-identical content — the same
    # request shape a `curl --data` call would make.
    raw_body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + os.environ.get(
            "AIFORGE_CODEMEM_LM_KEY", "lm-studio"),
        "Content-Type": "application/json",
        # Force Connection: close so mlx-lm sees one-shot requests.
        # httpx default keep-alive + connection pooling triggers
        # "Connection reset by peer" mid-stream on mlx-lm 0.31.
        "Connection": "close",
    }

    for attempt in range(RETRY_MAX + 1):
        try:
            return _post_summary(raw_body, headers)
        except httpx.HTTPStatusError:
            raise
        except Exception:  # noqa: BLE001 — transport, retried below
            if attempt >= RETRY_MAX:
                raise
            time.sleep(RETRY_BACKOFF_S)
    raise RuntimeError("symbol_summary._call_llm: unreachable")
