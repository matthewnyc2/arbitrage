"""NegRisk opportunity engine.

On every book update for a token that belongs to a known event, recompute the
sum of VWAP best-asks across all outcomes of that event and emit an
`Opportunity` when the depth-clipped net edge exceeds the threshold.

The math deliberately walks the book rather than trusting top-of-book — the
executable edge on a 100-share basket is often smaller than the quoted top.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from loguru import logger

from ..book.l2 import BookRegistry, BookUpdate, LiveBook
from ..config import settings
from ..models import Event, Opportunity, OpportunityLeg

BPS = Decimal(10_000)


@dataclass(frozen=True, slots=True)
class EngineConfig:
    min_net_edge_bps: int
    fees_per_share_usd: Decimal
    gas_per_basket_usd: Decimal
    max_basket_usd: Decimal
    min_basket_count: Decimal = Decimal(1)
    size_grid: tuple[Decimal, ...] = (
        Decimal(10),
        Decimal(25),
        Decimal(50),
        Decimal(100),
        Decimal(250),
        Decimal(500),
        Decimal(1000),
    )

    @classmethod
    def from_settings(cls) -> EngineConfig:
        return cls(
            min_net_edge_bps=settings.min_net_edge_bps,
            # CLOB taker fee is typically 0 on Polymarket; keep a hook for non-zero.
            fees_per_share_usd=Decimal("0.0"),
            # Approx sum of gas for split/buy legs + redeem on Polygon, USD-denominated.
            # Sized conservatively at ~$0.10 until we wire a real gas oracle.
            gas_per_basket_usd=Decimal("0.10"),
            max_basket_usd=settings.max_basket_usd,
        )


@dataclass
class EventIndex:
    """Registry mapping token_id -> Event so engine can look up siblings."""

    by_event_id: dict[str, Event] = field(default_factory=dict)
    by_token_id: dict[str, str] = field(default_factory=dict)

    def upsert(self, event: Event) -> None:
        self.by_event_id[event.id] = event
        for o in event.outcomes:
            self.by_token_id[o.token_id] = event.id

    def event_for_token(self, token_id: str) -> Event | None:
        eid = self.by_token_id.get(token_id)
        return self.by_event_id.get(eid) if eid else None


class OpportunityEngine:
    def __init__(
        self,
        *,
        books: BookRegistry,
        index: EventIndex,
        config: EngineConfig | None = None,
    ) -> None:
        self._books = books
        self._index = index
        self._config = config or EngineConfig.from_settings()
        self._out: asyncio.Queue[Opportunity] = asyncio.Queue(maxsize=256)

    @property
    def config(self) -> EngineConfig:
        return self._config

    async def run(self) -> None:
        async for update in self._books.updates():
            try:
                self._handle(update)
            except Exception as exc:
                logger.exception("engine handler failed: {}", exc)

    def _handle(self, update: BookUpdate) -> None:
        event = self._index.event_for_token(update.token_id)
        if event is None:
            return
        opp = self.evaluate(event)
        if opp is not None:
            self._out.put_nowait(opp)

    async def opportunities(self) -> AsyncIterator[Opportunity]:
        while True:
            yield await self._out.get()

    def evaluate(self, event: Event) -> Opportunity | None:
        legs_books: list[tuple[int, str, str, LiveBook]] = []
        for o in event.outcomes:
            book = self._books.get(o.token_id)
            if book is None or not book.asks:
                return None
            legs_books.append((o.outcome_index, o.token_id, o.name, book))

        best_opp: Opportunity | None = None
        best_profit = Decimal("-1")
        for candidate_k in self._candidate_sizes(legs_books):
            legs, cost_sum = self._walk_legs(legs_books, candidate_k)
            if legs is None:
                continue
            gross = Decimal(1) * candidate_k - cost_sum  # per-basket gross = 1 - Σ vwap
            gross_per_basket = gross / candidate_k
            n = Decimal(len(legs))
            fee_cost_per_basket = self._config.fees_per_share_usd * n
            gas_amortized = self._config.gas_per_basket_usd / candidate_k
            net_per_basket = gross_per_basket - fee_cost_per_basket - gas_amortized
            if net_per_basket <= 0:
                continue
            bps = int((net_per_basket / Decimal(1)) * BPS)
            if bps < self._config.min_net_edge_bps:
                continue
            expected_profit = net_per_basket * candidate_k
            if expected_profit <= best_profit:
                continue

            candidate = Opportunity.from_legs(
                detected_at=datetime.now(UTC),
                event=event,
                legs=tuple(legs),
                fees_per_share=self._config.fees_per_share_usd,
                gas_per_basket_usd=gas_amortized,
                max_baskets=candidate_k,
            )
            best_opp = candidate
            best_profit = expected_profit
        return best_opp

    def _candidate_sizes(
        self, legs_books: list[tuple[int, str, str, LiveBook]]
    ) -> list[Decimal]:
        depth = min(sum(lb.asks.values()) for *_, lb in legs_books)
        if depth <= 0:
            return []
        est_cost_per_share = sum(
            (lb.asks.keys()[0] for *_, lb in legs_books), Decimal(0)
        )
        budget_cap = (
            self._config.max_basket_usd / est_cost_per_share
            if est_cost_per_share > 0
            else depth
        )
        ceiling = min(depth, budget_cap)
        if ceiling < self._config.min_basket_count:
            return []
        candidates = [s for s in self._config.size_grid if s <= ceiling]
        if not candidates or candidates[-1] != ceiling:
            candidates.append(ceiling)
        return candidates

    def _walk_legs(
        self,
        legs_books: list[tuple[int, str, str, LiveBook]],
        size: Decimal,
    ) -> tuple[list[OpportunityLeg] | None, Decimal]:
        legs: list[OpportunityLeg] = []
        total_cost = Decimal(0)
        for idx, token_id, name, book in legs_books:
            res = book.vwap_buy(size)
            if res is None:
                return None, Decimal(0)
            vwap, filled, levels = res
            if filled < size:
                return None, Decimal(0)
            total_cost += vwap * size
            legs.append(
                OpportunityLeg(
                    token_id=token_id,
                    outcome_name=name,
                    outcome_index=idx,
                    vwap_price=vwap,
                    size=size,
                    levels_consumed=levels,
                )
            )
        return legs, total_cost
