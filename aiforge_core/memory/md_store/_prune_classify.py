"""md_store internals: tell run narration, toy-task facts and off-topic facts
apart from knowledge. Pure text rules, no model, no I/O — the passes that act
on them live in ``_prune``.

Narration is what a run SAID about itself ("pipeline complete 4/4", "created
calc.py", "all 20 tests pass", a /tmp path from one sandbox) — true once, never
useful to recall. A toy-task fact is about a practice exercise (calculator,
is_prime, fizzbuzz, hello world …) that a bench or a smoke test asked for.
"""
from __future__ import annotations

import re

# A sentence carrying one of these is stating a rule or a condition — keep it
# even when it also mentions a test run or a file ("tests pass only when NATS
# is up", "the user asked that commits are never automatic").
_CUE_RE = re.compile(
    r"\b(?:must|should|never|always|only|unless|requires?|need(?:s|ed)?|"
    r"prefer(?:s|red)?|because|otherwise|instead|avoid|don'?t|do not)\b", re.I)

_FILE = r"`?(?:[\w.-]+/)*[\w-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|c|h|cpp|rb|sh|md|txt|json|ya?ml|toml|html|css|sql|kt)`?"

# Always narration, whatever else the line says.
_HARD = [
    re.compile(r"^\[task-history\]|\bDID:", re.I),
    re.compile(r"\bpipeline (?:is )?(?:complete|completed|finished|done|failed)\b", re.I),
    re.compile(r"\b\d+/\d+ (?:sub)?(?:tasks?|steps?|stages?)\b", re.I),
    re.compile(r"^tools used:", re.I),
    re.compile(r"(?:^|[\s`'\"(=])/(?:tmp|private/tmp|var/folders)/"),
    re.compile(r"^current working directory is\b", re.I),
    re.compile(r"/\.worktrees/[\w.-]+"),
]
# Narration unless the line also states a rule (_CUE_RE).
_SOFT = [
    # "Created calc.py …", "Added test_x.py with 6 tests"
    re.compile(r"^(?:i\s+)?(?:created|added|wrote|edited|updated|modified|saved|"
               r"deleted|removed|renamed|implemented|built)\b.{0,80}?" + _FILE, re.I),
    # "file created at X", "X was added to Y.py"
    re.compile(r"\b(?:was|were|been) (?:created|added|written|edited|updated|"
               r"modified|saved)\b.{0,60}?" + _FILE, re.I),
    re.compile(r"\bfile (?:created|written|saved) (?:at|to|in)\b", re.I),
    # test counts / run status
    re.compile(r"\b\d+\s+(?:new\s+)?(?:pytest\s+)?(?:unit\s+)?(?:tests?|test cases?)\b"
               r".{0,60}\b(?:pass|passed|passing|added|written|created|green)\b", re.I),
    re.compile(r"\ball (?:\d+ )?(?:the )?tests? (?:pass|passed|passing|green)\b", re.I),
    re.compile(r"\b\d+ (?:passed|failed)\b", re.I),
    re.compile(r"[—–-]\s*all (?:passed|passing|pass)\b", re.I),
    re.compile(r"\bverified (?:with|by)\b|^verified\b|\bcompil(?:ation|ed) succeeded\b", re.I),
    re.compile(r"^(?:built|done|completed|finished|merged)\b.{0,40}$", re.I),
    # session events
    re.compile(r"\b(?:the |a )?user (?:asked|requested|submitted|said|typed|sent)\b", re.I),
    re.compile(r"\bsession ended\b", re.I),
    re.compile(r"^on \d{4}-\d{2}-\d{2}\b.{0,30}\b(?:user|system|agent)\b", re.I),
]

# Practice exercises a bench / smoke run asks for. Matched on whole words.
_TOY_RE = re.compile(
    r"\b(?:calc(?:ulator)?(?:\.py)?|is_?prime|primes?|fibonacci|fib(?:cache)?|"
    r"factorial|palindrome|is_palindrome|fizz_?buzz|hello[ _-]?world|hello\.(?:py|txt)|"
    r"slugify|todo(?:store|s|\.py)?|test_todo|celsius_to_fahrenheit|"
    r"fahrenheit_to_celsius|temperature\.py|reverse_string|counter class|"
    r"greet(?:ing)?|add\(a,\s*b\)|subtract\(a,\s*b\)|multiply\(a,\s*b\)|"
    r"divide\(a,\s*b\))", re.I)


def is_narration(item: str) -> bool:
    t = " ".join(str(item or "").split())
    if not t:
        return False
    t = re.sub(r"^\[[\w .:/-]{1,60}\]\s*", "", t) if not t.startswith("[task-history]") else t
    if any(rx.search(t) for rx in _HARD):
        return True
    if _CUE_RE.search(t):
        return False
    return any(rx.search(t) for rx in _SOFT)


def is_toy(item: str) -> bool:
    return bool(_TOY_RE.search(str(item or "")))


# ── off-topic detection ─────────────────────────────────────────────────────
# Words too common in this store to say two facts are about the same thing.
_STOP = frozenset("""
about above after again against also among another any are around based been
before being below between both but can cannot could default does doing done
down during each either else even every from further have having here into
its itself just like made make many more most much must need none not now
off once only other ours over own same should since some such than that the
their them then there these they this those through under until upon used
uses using very want was were what when where which while who whom why will
with within without would your yes you all and for has had how per via via
add adds added file files function functions method methods class classes
code value values return returns returned string strings list lists true
false none null test tests tested testing python java new old run runs
running ran set sets get gets call calls called data type types name names
line lines field fields item items number numbers support supports
supported include includes including provides provide provided create
created creates update updated work works working use user users system
""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def terms(text: str) -> set[str]:
    """Distinctive terms: words of 4+ letters, split on non-letters and
    camelCase, lower-cased, lightly stemmed, common words removed. A camelCase
    identifier also counts as its joined form (AiForgeMemory → aiforgememory)."""
    raw = str(text or "")
    out: set[str] = set()
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9]*", raw):
        parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", tok) or [tok]
        for w in [tok, *parts]:
            w = w.lower()
            if len(w) >= 4 and w not in _STOP:
                out.add(_stem(w))
    return out


# Bucket names, not topics: every kind of fact lands in them on purpose.
_BUCKET_TOPICS = frozenset("""
shared global misc general notes note knowledge memory learning learnings chat
prefs preferences rules rule session sessions task tasks project projects
user team other
""".split())


def off_topic(topic: str, items: list[str], *, min_items: int = 6) -> list[int]:
    """Indexes of items that share no distinctive term with the topic name nor,
    transitively, with any item that does.

    Conservative by construction: nothing is returned for a bucket name
    (shared, global, chat …), when no item names the topic (a repo brief whose
    facts never say the repo name), when there are fewer than ``min_items``
    items, or when the unrelated items would be a third of the brief or more
    (then it is the topic name that is off, not the facts). An item that states
    a rule (must / never / CONSTRAINT …) or has fewer than three distinctive
    terms (``MAX_CONCURRENT_MESSAGES=4``) is never called off-topic: too little
    text to judge."""
    topic = re.sub(r"(?:-\d+)+$", "", str(topic or "").strip())
    if len(items) < min_items or topic.lower() in _BUCKET_TOPICS:
        return []
    topic_terms = terms(topic) | terms(re.sub(r"[^A-Za-z0-9]", "", topic))
    if not topic_terms:
        return []
    joined = re.sub(r"[^a-z0-9]", "", topic.lower())
    tsets = [terms(x) for x in items]
    flat = [re.sub(r"[^a-z0-9]", "", x.lower()) for x in items]
    reached = {i for i, ts in enumerate(tsets)
               if ts & topic_terms or (len(joined) >= 5 and joined in flat[i])}
    if not reached:
        return []
    frontier = list(reached)
    while frontier:
        i = frontier.pop()
        for j, ts in enumerate(tsets):
            if j not in reached and ts & tsets[i]:
                reached.add(j)
                frontier.append(j)
    off = [i for i in range(len(items)) if i not in reached]
    if 3 * len(off) >= len(items):
        return []
    return [i for i in off if len(tsets[i]) >= 3
            and not _CUE_RE.search(items[i])
            and not re.match(r"^\s*(?:CONSTRAINT|RULE|PREF)\b", items[i])]
