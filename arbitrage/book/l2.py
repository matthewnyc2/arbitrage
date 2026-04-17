"""In-memory L2 order book per token.

Kept deliberately small: a sorted dict per side (price -> size), plus a bit of
bookkeeping to detect desync. The opportunity engine consumes immutable
`Book` pydantic snapshots rendered from this structure on every update.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Self

from sortedcontainers import SortedDict

from ..models import Book, BookLevel

REMOVE_AT_ZERO = Decimal(0)


class Side(str, Enum):
    BID = "BID"
    ASK = "ASK"


def _to_decimal(v: object) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        return Decimal(v)
    raise TypeError(f"cannot coerce {type(v).__name__} to Decimal")


def _coerce_side(v: object) -> Side:
    if isinstance(v, Side):
        return v
    s = str(v).strip().upper()
    if s in ("BUY", "BID", "B"):
        return Side.BID
    if s in ("SELL", "ASK", "S", "A"):
        return Side.ASK
    raise ValueError(f"unknown side: {v!r}")


@dataclass(slots=True)
class LevelChange:
    price: Decimal
    size: Decimal
    side: Side

    @classmethod
    def from_raw(cls, raw: dict[str, object]) -> Self:
        return cls(
            price=_to_decimal(raw["price"]),
            size=_to_decimal(raw["size"]),
            side=_coerce_side(raw["side"]),
        )


@dataclass(slots=True)
class LiveBook:
    """Mutable L2 book for one token. Bids and asks are sorted price ladders."""

    token_id: str
    bids: SortedDict = field(default_factory=SortedDict)  # price (Decimal) -> size (Decimal)
    asks: SortedDict = field(default_factory=SortedDict)
    snapshots_applied: int = 0
    deltas_applied: int = 0
    last_update: datetime | None = None
    last_hash: str | None = None

    def apply_snapshot(
        self,
        *,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        timestamp: datetime | None = None,
        book_hash: str | None = None,
    ) -> None:
        self.bids.clear()
        self.asks.clear()
        for price, size in bids:
            if size > 0:
                self.bids[price] = size
        for price, size in asks:
            if size > 0:
                self.asks[price] = size
        self.snapshots_applied += 1
        self.last_update = timestamp or datetime.now(UTC)
        self.last_hash = book_hash

    def apply_delta(
        self,
        changes: list[LevelChange],
        *,
        timestamp: datetime | None = None,
        book_hash: str | None = None,
    ) -> None:
        for ch in changes:
            side_map = self.bids if ch.side is Side.BID else self.asks
            if ch.size <= REMOVE_AT_ZERO:
                side_map.pop(ch.price, None)
            else:
                side_map[ch.price] = ch.size
        self.deltas_applied += 1
        self.last_update = timestamp or datetime.now(UTC)
        self.last_hash = book_hash

    def best_bid(self) -> tuple[Decimal, Decimal] | None:
        if not self.bids:
            return None
        price = self.bids.keys()[-1]
        return (price, self.bids[price])

    def best_ask(self) -> tuple[Decimal, Decimal] | None:
        if not self.asks:
            return None
        price = self.asks.keys()[0]
        return (price, self.asks[price])

    def vwap_buy(self, target_size: Decimal) -> tuple[Decimal, Decimal, int] | None:
        """Walk asks ascending to fill `target_size`.
        Returns (vwap_price, filled_size, levels_consumed). If unable to fill any, None.
        """
        if target_size <= 0 or not self.asks:
            return None
        remaining = target_size
        cost = Decimal(0)
        filled = Decimal(0)
        levels = 0
        for price in self.asks.keys():
            size = self.asks[price]
            take = min(remaining, size)
            cost += take * price
            filled += take
            levels += 1
            remaining -= take
            if remaining <= 0:
                break
        if filled <= 0:
            return None
        return (cost / filled, filled, levels)

    def to_snapshot(self) -> Book:
        bids = [
            BookLevel(price=p, size=s)
            for p, s in reversed(list(self.bids.items()))  # descending
        ]
        asks = [BookLevel(price=p, size=s) for p, s in self.asks.items()]
        return Book(
            token_id=self.token_id,
            bids=bids,
            asks=asks,
            sequence=self.snapshots_applied + self.deltas_applied,
            updated_at=self.last_update or datetime.now(UTC),
        )


@dataclass(slots=True)
class BookUpdate:
    """Emitted whenever a token's book is mutated."""

    token_id: str
    reason: str  # "snapshot" | "delta"
    at: datetime


class BookRegistry:
    """Holds LiveBooks and multiplexes update events to subscribers."""

    def __init__(self) -> None:
        self._books: dict[str, LiveBook] = {}
        self._subscribers: list[asyncio.Queue[BookUpdate]] = []

    def book(self, token_id: str) -> LiveBook:
        book = self._books.get(token_id)
        if book is None:
            book = LiveBook(token_id=token_id)
            self._books[token_id] = book
        return book

    def get(self, token_id: str) -> LiveBook | None:
        return self._books.get(token_id)

    def tokens(self) -> list[str]:
        return list(self._books.keys())

    def apply_snapshot(
        self,
        token_id: str,
        *,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        timestamp: datetime | None = None,
        book_hash: str | None = None,
    ) -> None:
        self.book(token_id).apply_snapshot(
            bids=bids, asks=asks, timestamp=timestamp, book_hash=book_hash
        )
        self._publish(BookUpdate(token_id=token_id, reason="snapshot", at=datetime.now(UTC)))

    def apply_delta(
        self,
        token_id: str,
        changes: list[LevelChange],
        *,
        timestamp: datetime | None = None,
        book_hash: str | None = None,
    ) -> None:
        self.book(token_id).apply_delta(
            changes, timestamp=timestamp, book_hash=book_hash
        )
        self._publish(BookUpdate(token_id=token_id, reason="delta", at=datetime.now(UTC)))

    async def updates(self, maxsize: int = 1024) -> AsyncIterator[BookUpdate]:
        queue: asyncio.Queue[BookUpdate] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.remove(queue)

    def _publish(self, update: BookUpdate) -> None:
        for q in self._subscribers:
            try:
                q.put_nowait(update)
            except asyncio.QueueFull:
                # If a consumer can't keep up, drop — the engine uses the
                # *current* book state anyway, so missing a tick is harmless.
                pass
