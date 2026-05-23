"""Fixed prompt set for the omlx vs LM Studio bench.

Two domains (matching the two models under test):
  - coder: code-heavy prompts, suits qwen3-coder
  - general: reasoning/summarization, suits granite-4.1

Each phase pulls from these:
  A (cold single)     -> SINGLE_PROMPTS[domain]                 (5 items)
  B (multi-turn)      -> MULTITURN_CONVOS[domain]               (3 conversations, 4 turns each, shared prefix)
  C (concurrent)      -> CONCURRENT_BATCHES[domain]             (3 batches of 4 distinct prompts)
"""

from __future__ import annotations

CODER_SYS = (
    "You are a senior software engineer. Reply with concise, correct code. "
    "When asked to modify code, return the full updated function."
)

GENERAL_SYS = (
    "You are a careful analyst. Answer in plain prose, no lists unless requested, "
    "and cite which sentence in the source supports your answer when relevant."
)

# Shared 1.5K-token-ish prefix for phase B (paste once, omlx should cache it).
CODER_CONTEXT = '''The following is the current state of a Python module. All
subsequent questions refer to this code. Do not repeat it back; just answer.

```python
# inventory_sync.py
from __future__ import annotations
import asyncio, time, logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable

log = logging.getLogger(__name__)


@dataclass
class Item:
    sku: str
    qty: int
    location: str
    updated_at: float = field(default_factory=time.time)


class InventoryStore:
    def __init__(self, name: str) -> None:
        self.name = name
        self._items: dict[str, Item] = {}
        self._lock = asyncio.Lock()

    async def upsert(self, item: Item) -> None:
        async with self._lock:
            existing = self._items.get(item.sku)
            if existing and existing.updated_at >= item.updated_at:
                return  # stale write, drop
            self._items[item.sku] = item

    async def delete(self, sku: str) -> bool:
        async with self._lock:
            return self._items.pop(sku, None) is not None

    async def snapshot(self) -> list[Item]:
        async with self._lock:
            return list(self._items.values())


class SyncPipeline:
    def __init__(self, src: InventoryStore, dst: InventoryStore, batch: int = 50) -> None:
        self.src, self.dst, self.batch = src, dst, batch
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        while not self._stopped.is_set():
            items = await self.src.snapshot()
            for chunk in self._chunked(items, self.batch):
                await asyncio.gather(*(self.dst.upsert(i) for i in chunk))
            await asyncio.sleep(2.0)

    @staticmethod
    def _chunked(seq: Iterable[Item], n: int) -> Iterator[list[Item]]:
        buf: list[Item] = []
        for x in seq:
            buf.append(x)
            if len(buf) == n:
                yield buf
                buf = []
        if buf:
            yield buf

    def stop(self) -> None:
        self._stopped.set()
```

It is used by two cron-style coroutines feeding into a Postgres mirror.
'''

GENERAL_CONTEXT = '''The following is a 700-word product brief. All subsequent
questions refer to it. Do not summarize it unprompted; just answer the user.

== BRIEF ==
NimbusPay is a card-present terminal acquirer targeting SMBs in three
geographies: Brazil, Mexico, and Colombia. Founded in 2021, it ships its own
Android-based PIN-pad ("Cumulus T1") and a cloud settlement stack that batches
authorizations every 90 seconds. The thesis is that vertical hardware plus
batched settlement undercuts incumbent acquirers by 40-60 bps per swipe.

Q3 2025 milestones: 18,400 active terminals, GMV USD 412M, take rate 1.32%,
contribution margin (post-interchange, post-fraud) 28 bps. Churn at the
merchant level is 4.1% monthly, driven mostly by terminal outages in regions
where the SIM fallback (Movistar, Tigo) is unreliable.

Q4 priorities, in order: (1) ship the dual-SIM Cumulus T2 with offline-store
support up to 200 swipes per terminal, (2) launch a working-capital product
("NimbusFlex") underwritten on swipe history, (3) onboard one acquiring BIN
sponsor in Colombia (currently routed through a partner). The PRD identifies
three risks: chargeback reserve mismatch (current ratio 0.7% vs regulator
expectation 1.0%), engineering capacity (settlement team is 6 FTE for a
22-service estate), and the offline-store edge cases (double-swipe, clock
drift, expired-card timing attacks).

Competition: Stone, Cielo, PagBank in Brazil; Clip, Conekta, Kushki in Mexico
and Colombia. Differentiation cited: 90s settlement (peers: T+1), Android
terminal SDK (peers: locked firmware), local FX hedging on USD-priced fees.
The brief notes that the 90s settlement claim is unverified when the merchant
acquiring BIN is partner-sponsored, which today is the case in Colombia.

Funding: Series B closed Q1 2025, USD 38M, post-money 240M. Lead: Valor; co:
Kaszek, Quona. Runway 22 months at current burn (USD 1.4M/mo). Next milestone
expected to support a Series C is 50,000 active terminals and contribution
margin >= 40 bps.
== END BRIEF ==
'''


SINGLE_PROMPTS = {
    "coder": [
        "Refactor `SyncPipeline._chunked` to be an async generator that yields chunks lazily without loading all items into memory.",
        "Identify the race condition between `upsert` and `snapshot` in `InventoryStore` and propose a fix that does not regress write throughput.",
        "Write a pytest test that verifies `upsert` correctly drops a stale write (older `updated_at`).",
        "Add a `since: float | None` parameter to `snapshot` that returns only items updated at or after that timestamp.",
        "The `SyncPipeline._chunked` annotation says `Iterator[list[Item]]` but `Iterator` is not imported. Show the minimal correct fix.",
    ],
    "general": [
        "What is NimbusPay's Q4 priority order and which one is most at risk given the named engineering capacity?",
        "Why does the brief flag the 90-second settlement claim as 'unverified' for Colombia, and what would resolve it?",
        "Compute the GMV per active terminal for Q3 2025 and compare it to a back-of-envelope target needed for the Series C contribution-margin goal.",
        "Which of the three Q4 risks is most likely to surface first in production, and what is the leading indicator?",
        "Summarize the merchant-churn root cause in one sentence and propose the most direct mitigation supported by the brief.",
    ],
}


# Phase B: 3 conversations per domain. Each is (system, context, [turn1, turn2, turn3, turn4]).
# Key: turn 1 starts cold; turns 2-4 must reuse the cached prefix (system + context + earlier turns)
# if the server's KV cache works.
MULTITURN_CONVOS = {
    "coder": [
        {
            "system": CODER_SYS,
            "context": CODER_CONTEXT,
            "turns": [
                "Where is the bug that causes `snapshot` to occasionally return items that no longer exist in the source store, under concurrent writers?",
                "Now show the patched `snapshot` method.",
                "What is the worst-case latency cost of your fix when the store holds 1M items?",
                "Add a fast path that skips locking when no writer has touched the store since the last snapshot.",
            ],
        },
        {
            "system": CODER_SYS,
            "context": CODER_CONTEXT,
            "turns": [
                "Add an async iterator on `InventoryStore` that yields items as they are upserted, without polling.",
                "Show how a consumer would back-pressure that iterator if it cannot keep up.",
                "What guarantees does your iterator give about ordering and duplicates?",
                "Rewrite the iterator to be cancellation-safe so cancelling the consumer releases the producer's queue slot.",
            ],
        },
        {
            "system": CODER_SYS,
            "context": CODER_CONTEXT,
            "turns": [
                "Convert `SyncPipeline.run` so it exits cleanly within 100 ms of `stop()` being called, instead of waiting up to 2 s in `asyncio.sleep`.",
                "Now make the sleep duration configurable via constructor.",
                "Add structured logging at the start and end of each batch including chunk count and elapsed ms.",
                "Wrap the batch loop in a single span using the `opentelemetry` SDK style (no real import needed, just the shape).",
            ],
        },
    ],
    "general": [
        {
            "system": GENERAL_SYS,
            "context": GENERAL_CONTEXT,
            "turns": [
                "What is NimbusPay's contribution margin in Q3 2025 in basis points, and how far is it from the Series C target?",
                "Given the runway and burn, what monthly contribution-margin trajectory closes the gap before runway expires?",
                "Which of the three Q4 priorities most directly drives that trajectory, and why?",
                "If NimbusFlex is delayed by one quarter, how does your answer change?",
            ],
        },
        {
            "system": GENERAL_SYS,
            "context": GENERAL_CONTEXT,
            "turns": [
                "Restate the chargeback reserve risk in plain language for a non-technical board member.",
                "What single number would you ask the CFO to send weekly to track it?",
                "What is the regulatory consequence if the gap is not closed before Series C diligence?",
                "Propose the cheapest near-term fix that does not require new capital.",
            ],
        },
        {
            "system": GENERAL_SYS,
            "context": GENERAL_CONTEXT,
            "turns": [
                "Which competitor is the most credible threat per geography, and on what dimension?",
                "Does the brief support that ranking with hard numbers? If not, what is the supporting evidence?",
                "What single feature gap, if closed, would neutralize the strongest threat in Brazil?",
                "How long would shipping that feature take given the stated engineering capacity?",
            ],
        },
    ],
}


# Phase C: 3 batches of 4 prompts each. Independent prompts, no shared prefix —
# this isolates the continuous-batching effect from the KV-cache effect.
CONCURRENT_BATCHES = {
    "coder": [
        SINGLE_PROMPTS["coder"][:4],
        SINGLE_PROMPTS["coder"][1:5],
        [
            "Write a one-liner that returns the SKU of the most recently updated item across an `InventoryStore` snapshot.",
            "Convert the `Item` dataclass to use `slots=True` and explain when that matters.",
            "Write a property-based hypothesis test that `upsert` is idempotent for equal `updated_at`.",
            "What is the BigO of `snapshot` and how would you reduce it if items >> 100k?",
        ],
    ],
    "general": [
        SINGLE_PROMPTS["general"][:4],
        SINGLE_PROMPTS["general"][1:5],
        [
            "In one sentence, what is NimbusPay's moat per the brief?",
            "Name the single largest unverified claim in the brief.",
            "If you were a Series C lead, what is your first diligence question?",
            "What is the implied Series C valuation if the company hits its stated milestones at a 6x revenue multiple?",
        ],
    ],
}


def context_for(domain: str) -> str:
    return CODER_CONTEXT if domain == "coder" else GENERAL_CONTEXT


def system_for(domain: str) -> str:
    return CODER_SYS if domain == "coder" else GENERAL_SYS
