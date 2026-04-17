"""Paper-fill executor.

When an opportunity arrives, snapshot the book, wait `paper_latency_ms` to
model getting beaten by faster bots, and only fill against levels that survive
that delay. The simulator biases PnL *downward* relative to naive "fill at
observation time" paper trading.

Persistence: writes one `baskets` row (is_paper=1) plus one `fills` row per leg.
Status transitions:
  - detected -> open (created, legs in flight)
  - all legs filled at size -> pending_resolution
  - any leg short -> failed (persisted for forensics; no resolution step)
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from loguru import logger

from ..book.l2 import BookRegistry, LiveBook
from ..config import settings
from ..db import db_conn
from ..models import Basket, BasketStatus, Fill, Opportunity, Side


@dataclass(slots=True)
class PaperFillResult:
    filled: Decimal
    vwap_price: Decimal
    levels_consumed: int


def simulate_leg_fill(book: LiveBook, target_size: Decimal) -> PaperFillResult:
    """Walk `book.asks` at the *current* moment and fill up to `target_size`.
    Returns the actually-filled size (may be < target_size if depth vanished).
    """
    if target_size <= 0 or not book.asks:
        return PaperFillResult(Decimal(0), Decimal(0), 0)
    remaining = target_size
    cost = Decimal(0)
    filled = Decimal(0)
    levels = 0
    for price in list(book.asks.keys()):
        size = book.asks.get(price, Decimal(0))
        if size <= 0:
            continue
        take = min(remaining, size)
        cost += take * price
        filled += take
        levels += 1
        remaining -= take
        if remaining <= 0:
            break
    vwap = (cost / filled) if filled > 0 else Decimal(0)
    return PaperFillResult(filled, vwap, levels)


class PaperExecutor:
    """Runs fill simulations + persistence for paper baskets."""

    def __init__(
        self,
        *,
        books: BookRegistry,
        latency_ms: int | None = None,
        fee_rate: Decimal = Decimal(0),
    ) -> None:
        self._books = books
        self._latency_ms = latency_ms if latency_ms is not None else settings.paper_latency_ms
        self._fee_rate = fee_rate

    @property
    def latency_ms(self) -> int:
        return self._latency_ms

    async def execute(self, opp: Opportunity) -> Basket | None:
        await asyncio.sleep(self._latency_ms / 1000.0)
        return await self._simulate_and_persist(opp)

    async def execute_now(self, opp: Opportunity) -> Basket | None:
        """Skip the sleep — used by tests that want deterministic fills."""
        return await self._simulate_and_persist(opp)

    async def _simulate_and_persist(self, opp: Opportunity) -> Basket | None:
        target = opp.max_baskets
        if target <= 0:
            return None

        fills: list[Fill] = []
        total_cost = Decimal(0)
        short_legs = 0
        min_filled: Decimal | None = None
        now = datetime.now(UTC)

        for leg in opp.legs:
            book = self._books.get(leg.token_id)
            if book is None:
                short_legs += 1
                continue
            result = simulate_leg_fill(book, target)
            if result.filled < target:
                short_legs += 1
            if min_filled is None or result.filled < min_filled:
                min_filled = result.filled
            fee = result.vwap_price * result.filled * self._fee_rate
            fills.append(
                Fill(
                    token_id=leg.token_id,
                    side=Side.BUY,
                    price=result.vwap_price,
                    size=result.filled,
                    fee_usd=fee,
                    filled_at=now,
                )
            )
            total_cost += result.vwap_price * result.filled + fee

        basket_count = min_filled if min_filled is not None else Decimal(0)
        if short_legs > 0 or basket_count <= 0:
            status = BasketStatus.FAILED
        else:
            status = BasketStatus.PENDING_RESOLUTION

        basket = Basket(
            opportunity_id=opp.id,
            event_id=opp.event_id,
            is_paper=True,
            created_at=now,
            basket_count=basket_count,
            total_cost_usd=total_cost,
            status=status,
            fills=fills,
        )
        await self._persist(opp, basket)
        logger.info(
            "paper basket {} status={} count={} cost={}",
            basket.id,
            basket.status.value,
            basket.basket_count,
            basket.total_cost_usd,
        )
        return basket

    async def _persist(self, opp: Opportunity, basket: Basket) -> None:
        legs_json = json.dumps([leg.model_dump(mode="json") for leg in opp.legs])
        async with db_conn() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO opportunities
                  (id, detected_at, event_id, event_title, sum_vwap_asks,
                   net_edge_bps, max_baskets, expected_profit_usd, legs_json, acted_on)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    opp.id,
                    opp.detected_at.isoformat(),
                    opp.event_id,
                    opp.event_title,
                    str(opp.sum_vwap_asks),
                    opp.net_edge_bps,
                    str(opp.max_baskets),
                    str(opp.expected_profit_usd),
                    legs_json,
                ),
            )
            await conn.execute(
                """
                INSERT INTO baskets
                  (id, opportunity_id, event_id, is_paper, created_at, basket_count,
                   total_cost_usd, status)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    basket.id,
                    basket.opportunity_id,
                    basket.event_id,
                    basket.created_at.isoformat(),
                    str(basket.basket_count),
                    str(basket.total_cost_usd),
                    basket.status.value,
                ),
            )
            for fill in basket.fills:
                await conn.execute(
                    """
                    INSERT INTO fills
                      (basket_id, token_id, side, price, size, fee_usd, filled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        basket.id,
                        fill.token_id,
                        fill.side.value,
                        str(fill.price),
                        str(fill.size),
                        str(fill.fee_usd),
                        fill.filled_at.isoformat(),
                    ),
                )
            await conn.commit()


async def mark_resolution(
    event_id: str,
    *,
    winning_token_id: str | None,
    resolved_at: datetime | None = None,
    source: str = "manual",
) -> int:
    """Apply a resolution to all open paper baskets for the event.

    Updates `resolutions` row, flips matching baskets to redeemed/invalid,
    and writes realized PnL. Returns number of baskets updated.
    """
    resolved_at = resolved_at or datetime.now(UTC)
    async with db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO resolutions (event_id, winning_outcome_token_id, resolved_at, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
              winning_outcome_token_id=excluded.winning_outcome_token_id,
              resolved_at=excluded.resolved_at,
              source=excluded.source
            """,
            (event_id, winning_token_id, resolved_at.isoformat(), source),
        )
        cursor = await conn.execute(
            """
            SELECT id, basket_count, total_cost_usd, status
            FROM baskets
            WHERE event_id=? AND is_paper=1 AND status=?
            """,
            (event_id, BasketStatus.PENDING_RESOLUTION.value),
        )
        rows = await cursor.fetchall()
        updated = 0
        for row in rows:
            basket_id = row[0]
            basket_count = Decimal(row[1])
            cost = Decimal(row[2])
            if winning_token_id is None:
                payout = Decimal(0)
                new_status = BasketStatus.INVALID.value
            else:
                payout = basket_count * Decimal(1)
                new_status = BasketStatus.REDEEMED.value
            pnl = payout - cost
            await conn.execute(
                """
                UPDATE baskets
                SET status=?, redeemed_at=?, redeemed_payout_usd=?, realized_pnl_usd=?
                WHERE id=?
                """,
                (
                    new_status,
                    resolved_at.isoformat(),
                    str(payout),
                    str(pnl),
                    basket_id,
                ),
            )
            updated += 1
        await conn.commit()
        return updated
