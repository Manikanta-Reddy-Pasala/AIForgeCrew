"""Ranked, personalised, window-budgeted repo map (pure python, no new deps).

Today's regex symbol map lists the first 200 files in walk order with a fixed
size.  This builds a reference graph (file A -> file B when A mentions a symbol
B defines, plus import-stem edges), ranks files with personalised PageRank
(power iteration), boosts what the run is working on (files read/edited,
@-mentions, identifiers in the user's message), and renders to a char budget
that scales with the free window: top files with their symbols, the rest as
bare names.

Speed / stability contract
  * the repo scan is incremental (per-file mtime+size cache) and bounded; the
    caller runs it behind a short join so a turn never waits long,
  * the rendered text is cached by a hash of its inputs and a follow-up turn
    reuses the previous text unless the personalisation changed materially, so
    the prompt prefix stays byte-identical and the server prompt cache survives,
  * any failure returns "" and the caller falls back to today's map.

Switches: AIFORGE_REPOMAP_RANK (default 1; 0 = old behaviour),
AIFORGE_REPOMAP_MAX_CHARS (hard cap, existing), AIFORGE_REPOMAP_FREE_FRAC
(share of the free window the map may use, default 0.15),
AIFORGE_REPOMAP_REFRESH_DELTA (focus changes needed to re-rank, default 3),
AIFORGE_REPOMAP_SCAN_MAX_S (scan deadline, default 20),
AIFORGE_REPOMAP_MAX_FILES (default 4000).
"""
from __future__ import annotations

import collections
import hashlib
import math
import os
import re
import threading
import time

from .._shell import _workspace_root
from .._tools import _SKIP_DIRS

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_IMPORT = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+)|"
    r"(?:import|export)\b[^'\"\n]*?from\s+['\"]([^'\"]+)['\"]|"
    r"require\(\s*['\"]([^'\"]+)['\"]\s*\)|"
    r"(?:package|import)\s+(?:static\s+)?([\w.]+);)", re.MULTILINE)
_MAX_IDENTS = 1500
_MAX_SYMS_KEPT = 40
_MAX_SYMS_SHOWN = 12
_DEFAULT_DEFINERS_CAP = 5     # a name defined in more files than this is noise


def rank_enabled() -> bool:
    return os.environ.get("AIFORGE_REPOMAP_RANK", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# ── scan (incremental) ──────────────────────────────────────────────────

# {base: {rel: (mtime_ns, size, syms, idents, imports)}}
_SCAN: dict = {}
_SCAN_LOCK = threading.Lock()


def _read_record(path: str, pattern, noise):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read(200_000)
    except Exception:  # noqa: BLE001
        return None
    syms: list[str] = []
    for m in pattern.finditer(src):
        nm = next((g for g in m.groups() if g), None)
        if nm and nm not in syms and nm not in noise:
            syms.append(nm)
            if len(syms) >= _MAX_SYMS_KEPT:
                break
    counts = collections.Counter(_IDENT.findall(src))
    idents = dict(counts.most_common(_MAX_IDENTS))
    imports = []
    for m in _IMPORT.finditer(src):
        mod = next((g for g in m.groups() if g), "")
        if mod:
            imports.append(re.split(r"[./]", mod.strip("./"))[-1] if mod else "")
    return syms, idents, [i for i in imports if i][:60]


def scan_repo(base: str, deadline: float | None = None) -> dict:
    """{rel: (mtime_ns, size, syms, idents, imports)} for every source file,
    reusing the previous record of any file whose mtime+size is unchanged."""
    import re as _re
    from ._repomap import _SYM_NOISE, _SYM_PATTERNS
    compiled = {e: _re.compile(p, _re.MULTILINE) for e, p in _SYM_PATTERNS.items()}
    max_files = int(_env_num("AIFORGE_REPOMAP_MAX_FILES", 4000))
    prev = _SCAN.get(base, {})
    out: dict = {}
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS
                         and not d.startswith("."))
        for f in sorted(files):
            pat = compiled.get(os.path.splitext(f)[1].lower())
            if pat is None:
                continue
            if len(out) >= max_files or (deadline and time.monotonic() > deadline):
                break
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            rel = os.path.relpath(fp, base)
            old = prev.get(rel)
            if old and old[0] == st.st_mtime_ns and old[1] == st.st_size:
                out[rel] = old
                continue
            rec = _read_record(fp, pat, _SYM_NOISE)
            if rec is not None:
                out[rel] = (st.st_mtime_ns, st.st_size, *rec)
        else:
            continue
        break
    with _SCAN_LOCK:
        _SCAN[base] = out
    return out


# ── graph + PageRank ────────────────────────────────────────────────────

def _ident_weight(name: str) -> float:
    """Aider's heuristic: a distinctive name (snake/camel/long) is a strong
    reference, a short lowercase word (`out`, `base`, `kind`) is mostly a local
    variable or common noun and barely counts; a private name counts less."""
    w = 1.0
    distinctive = ("_" in name.strip("_") or any(c.isupper() for c in name[1:])
                   or len(name) >= 9)
    w = 8.0 if distinctive else 0.1
    if name.startswith("_"):
        w *= 0.2
    return w


def build_graph(scan: dict) -> dict:
    """{src_idx: {dst_idx: weight}} over the sorted file list (returned as
    ``(files, edges)``)."""
    files = sorted(scan)
    index = {f: i for i, f in enumerate(files)}
    definers: dict[str, list[int]] = collections.defaultdict(list)
    stems: dict[str, list[int]] = collections.defaultdict(list)
    for f in files:
        i = index[f]
        for s in scan[f][2]:
            if len(s) > 3:
                definers[s].append(i)
        stems[os.path.splitext(os.path.basename(f))[0]].append(i)
    edges: dict[int, dict[int, float]] = {}
    for f in files:
        i = index[f]
        row: dict[int, float] = {}
        for ident, n in scan[f][3].items():
            ds = definers.get(ident)
            if not ds or len(ds) > _DEFAULT_DEFINERS_CAP:
                continue
            w = _ident_weight(ident) * math.sqrt(n) / len(ds)
            for j in ds:
                if j != i:
                    row[j] = row.get(j, 0.0) + w
        for stem in scan[f][4]:
            ds = stems.get(stem)
            if ds and len(ds) <= _DEFAULT_DEFINERS_CAP:
                for j in ds:
                    if j != i:
                        row[j] = row.get(j, 0.0) + 2.0 / len(ds)
        if row:
            edges[i] = row
    return {"files": files, "edges": edges}


def pagerank(n: int, edges: dict, personalization: list[float] | None = None,
             damping: float = 0.85, iters: int = 40, tol: float = 1e-7) -> list[float]:
    """Personalised PageRank by power iteration. Dangling mass goes back to the
    personalisation vector."""
    if n <= 0:
        return []
    p = personalization or [1.0] * n
    tot = sum(p) or 1.0
    p = [x / tot for x in p]
    out_w = {i: sum(r.values()) for i, r in edges.items()}
    rank = list(p)
    for _ in range(iters):
        new = [(1.0 - damping) * p[i] for i in range(n)]
        dangling = 0.0
        for i in range(n):
            ow = out_w.get(i)
            if not ow:
                dangling += rank[i]
                continue
            share = damping * rank[i] / ow
            for j, w in edges[i].items():
                new[j] += share * w
        if dangling:
            d = damping * dangling
            for i in range(n):
                new[i] += d * p[i]
        delta = sum(abs(new[i] - rank[i]) for i in range(n))
        rank = new
        if delta < tol:
            break
    return rank


# ── personalisation ─────────────────────────────────────────────────────

_FOCUS: dict = {}                 # {(session_id, base): OrderedDict(rel -> weight)}
_FOCUS_MAX = 64
_FOCUS_LOCK = threading.Lock()
_AT_MENTION = re.compile(r"@([\w./\-]+)")
_PATHISH = re.compile(r"[\w./\-]+\.[A-Za-z]{1,5}\b")


def note_focus(session_id, base: str, paths, weight: float = 1.0) -> None:
    """Record files the run read (weight 1) or edited (weight 2). Never raises;
    absolute paths are made relative to ``base``; foreign paths are dropped."""
    try:
        base = str(base)
        with _FOCUS_LOCK:
            d = _FOCUS.setdefault((session_id, base), collections.OrderedDict())
            for p in paths or ():
                p = str(p)
                if os.path.isabs(p):
                    if not p.startswith(base.rstrip(os.sep) + os.sep):
                        continue
                    p = os.path.relpath(p, base)
                d[p] = max(weight, d.pop(p, 0.0))     # move to newest end
            while len(d) > _FOCUS_MAX:
                d.popitem(last=False)
            if len(_FOCUS) > 64:
                _FOCUS.pop(next(iter(_FOCUS)))
    except Exception:  # noqa: BLE001
        pass


def focus_from_text(text: str, files: list[str], definers: set[str]) -> tuple[set, set]:
    """(mentioned_files, mentioned_idents): files the text names (@x, a path or
    a unique basename) and identifiers that are DEFINED somewhere in the repo
    (chat filler words are dropped, so they cannot churn the cache key)."""
    text = text or ""
    named: set[str] = set()
    by_base: dict[str, list[str]] = collections.defaultdict(list)
    for f in files:
        by_base[os.path.basename(f)].append(f)
    cands = set(_AT_MENTION.findall(text)) | set(_PATHISH.findall(text))
    for c in cands:
        c = c.strip("./")
        if c in by_base and len(by_base[c]) <= 3:
            named.update(by_base[c])
        else:
            named.update(f for f in files if f == c or f.endswith("/" + c))
    idents = {w for w in _IDENT.findall(text) if w in definers}
    return named, idents


def _personalization(files, scan, recorded, named, idents):
    index = {f: i for i, f in enumerate(files)}
    p = [1.0] * len(files)
    for rel, w in recorded.items():
        i = index.get(rel)
        if i is not None:
            p[i] += 15.0 * w
    for rel in named:
        i = index.get(rel)
        if i is not None:
            p[i] += 30.0
    if idents:
        for f in files:
            hit = sum(1 for s in scan[f][2] if s in idents)
            if hit:
                p[index[f]] += 10.0 * min(hit, 3)
    return p


# ── budget ──────────────────────────────────────────────────────────────

_BUCKET = 2048


def map_budget_chars(role=None, history_chars: int = 0,
                     sys_chars: int | None = None) -> int:
    """Chars the map may take: the lesser of the hard cap and a fraction of the
    window left after the system prompt, the reply reservation and the history.
    Quantised down to 2K steps so a few more history chars do not change the
    text (and bust the server prompt cache) every turn."""
    from ._repomap import _repomap_max_chars
    from ._window import (_CTX_BUDGET_FLOOR_CHARS, _SYSTEM_PROMPT_CHARS,
                          _history_fraction, _window_scaled, _window_tokens)
    env = os.environ.get("AIFORGE_REPOMAP_MAX_CHARS")
    hard = _repomap_max_chars()
    if env is None:
        hard = _window_scaled(hard, 0.04, role)       # room to grow when roomy
    if hard == 0:
        return 0
    win = _window_tokens(role) or 131072
    try:
        from aiforge_core.config import runtime_settings
        out_chars = int(runtime_settings.get("max_output_tokens")) * 4
    except Exception:  # noqa: BLE001
        out_chars = 4096 * 4
    win_chars = win * 4
    ceiling = min(int(win_chars * _history_fraction(role)), win_chars - out_chars)
    reserve = _SYSTEM_PROMPT_CHARS if sys_chars is None else max(0, int(sys_chars))
    free = max(ceiling - reserve - max(0, int(history_chars)),
               _CTX_BUDGET_FLOOR_CHARS)
    frac = min(0.5, max(0.01, _env_num("AIFORGE_REPOMAP_FREE_FRAC", 0.15)))
    budget = min(hard, int(free * frac))
    budget = (budget // _BUCKET) * _BUCKET
    return max(budget, 1500 if hard >= 1500 else hard)


# ── render + cache ──────────────────────────────────────────────────────

def render(files, scan, rank, budget: int) -> str:
    order = sorted(range(len(files)), key=lambda i: (-round(rank[i], 12), files[i]))
    full_cap = int(budget * 0.75)
    lines: list[str] = []
    used = 0
    for i in order:
        f = files[i]
        syms = scan[f][2][:_MAX_SYMS_SHOWN]
        if not syms:
            continue
        line = f"{f}: {', '.join(syms)}"
        if used + len(line) + 1 > full_cap:
            break
        lines.append(line)
        used += len(line) + 1
    shown = set()
    for ln in lines:
        shown.add(ln.split(": ", 1)[0])
    rest = [files[i] for i in order if files[i] not in shown]
    names: list[str] = []
    room = budget - used - 40
    omitted = 0
    for f in rest:
        if room - len(f) - 2 < 0:
            omitted += 1
            continue
        names.append(f)
        room -= len(f) + 2
    out = "\n".join(lines)
    if names:
        out += "\nOther files (names only): " + ", ".join(names)
    if omitted:
        out += f"\n… ({omitted} more files — grep/find for the rest)"
    return out


_BY_KEY: "collections.OrderedDict[str, str]" = collections.OrderedDict()
_LAST: dict = {}                  # {(session_id, base): (struct_key, budget, focus, text)}
_CACHE_MAX = 32
_CACHE_LOCK = threading.Lock()


def _struct_key(files, scan) -> str:
    h = hashlib.sha1()
    for f in files:
        h.update(f.encode())
        h.update(b"\0")
        h.update(",".join(scan[f][2][:_MAX_SYMS_SHOWN]).encode())
        h.update(b"\n")
    return h.hexdigest()


def ranked_map(base: str, *, user_text: str = "", session_id=None,
               budget: int = 0, deadline: float | None = None,
               scan: dict | None = None) -> str:
    """The ranked map text for ``base`` ("" when disabled/empty/failed).
    ``scan`` reuses an existing scan instead of walking the tree."""
    if not rank_enabled() or budget <= 0:
        return ""
    if scan is None:
        scan = scan_repo(base, deadline)
    if not scan:
        return ""
    files = sorted(scan)
    skey = _struct_key(files, scan)
    definers = {s for f in files for s in scan[f][2] if len(s) > 3}
    named, idents = focus_from_text(user_text, files, definers)
    with _FOCUS_LOCK:
        recorded = dict(_FOCUS.get((session_id, base), {}))
    recorded = {k: v for k, v in recorded.items() if k in scan}
    focus = frozenset(set(recorded) | named | {"#" + i for i in idents})
    lkey = (session_id, base)
    with _CACHE_LOCK:
        last = _LAST.get(lkey)
    delta = int(_env_num("AIFORGE_REPOMAP_REFRESH_DELTA", 3))
    if (last and last[0] == skey and last[1] == budget
            and len(last[2] ^ focus) < max(1, delta)):
        return last[3]                       # not a material change: same bytes
    ckey = hashlib.sha1(repr((skey, budget, sorted(focus))).encode()).hexdigest()
    with _CACHE_LOCK:
        text = _BY_KEY.get(ckey)
    if text is None:
        graph = build_graph(scan)
        pers = _personalization(files, scan, recorded, named, idents)
        rank = pagerank(len(files), graph["edges"], pers)
        text = render(files, scan, rank, budget)
        with _CACHE_LOCK:
            _BY_KEY[ckey] = text
            while len(_BY_KEY) > _CACHE_MAX:
                _BY_KEY.popitem(last=False)
    with _CACHE_LOCK:
        _LAST[lkey] = (skey, budget, focus, text)
        while len(_LAST) > _CACHE_MAX:
            _LAST.pop(next(iter(_LAST)))
    return text


# ── background job (bounded wait) ───────────────────────────────────────

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def warm_scan(base: str) -> dict:
    """Start (once) an incremental scan in the background; returns the job."""
    with _JOBS_LOCK:
        job = _JOBS.get(base)
        if job is not None and job["thread"].is_alive():
            return job
        job = {}

        def _work():
            try:
                scan_repo(base, time.monotonic()
                          + _env_num("AIFORGE_REPOMAP_SCAN_MAX_S", 20))
            except Exception:  # noqa: BLE001
                pass
        job["thread"] = threading.Thread(target=_work, daemon=True,
                                         name="aiforge-repomap-scan")
        _JOBS[base] = job
        job["thread"].start()
        return job


def ranked_map_bounded(base: str, *, user_text: str = "", session_id=None,
                       budget: int = 0, wait_s: float = 3.0) -> str:
    """:func:`ranked_map` with a short bound: wait at most ``wait_s`` for the
    (possibly warming) scan; not ready -> "" (the scan keeps going for the next
    turn). A scan that is already cached makes this effectively instant."""
    try:
        if not rank_enabled() or budget <= 0:
            return ""
        job = warm_scan(base)
        job["thread"].join(max(0.0, wait_s))
        scan = _SCAN.get(base)
        if not scan:                 # still scanning for the first time
            return ""
        return ranked_map(base, user_text=user_text, session_id=session_id,
                          budget=budget, scan=scan)
    except Exception:  # noqa: BLE001
        return ""


def reset_caches() -> None:
    with _CACHE_LOCK, _FOCUS_LOCK, _SCAN_LOCK, _JOBS_LOCK:
        _SCAN.clear(); _BY_KEY.clear(); _LAST.clear(); _FOCUS.clear(); _JOBS.clear()  # noqa: E702
