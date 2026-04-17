"""L2 book delta + registry tests."""
from __future__ import annotations

import asyncio
from decimal import Decimal as D

import pytest

from arbitrage.book.l2 import BookRegistry, LevelChange, LiveBook, Side


class TestLiveBook:
    def test_snapshot_sets_best_bid_ask(self) -> None:
        b = LiveBook(token_id="t")
        b.apply_snapshot(
            bids=[(D("0.10"), D("100")), (D("0.12"), D("50"))],
            asks=[(D("0.20"), D("80")), (D("0.19"), D("40"))],
        )
        assert b.best_bid() == (D("0.12"), D("50"))
        assert b.best_ask() == (D("0.19"), D("40"))

    def test_snapshot_drops_zero_size_levels(self) -> None:
        b = LiveBook(token_id="t")
        b.apply_snapshot(bids=[(D("0.10"), D("0"))], asks=[(D("0.20"), D("10"))])
        assert b.best_bid() is None

    def test_delta_removes_level_at_zero(self) -> None:
        b = LiveBook(token_id="t")
        b.apply_snapshot(bids=[], asks=[(D("0.50"), D("10")), (D("0.60"), D("5"))])
        b.apply_delta([LevelChange(price=D("0.50"), size=D("0"), side=Side.ASK)])
        assert b.best_ask() == (D("0.60"), D("5"))

    def test_delta_replaces_size_at_level(self) -> None:
        b = LiveBook(token_id="t")
        b.apply_snapshot(bids=[], asks=[(D("0.50"), D("10"))])
        b.apply_delta([LevelChange(price=D("0.50"), size=D("3"), side=Side.ASK)])
        assert b.best_ask() == (D("0.50"), D("3"))

    def test_vwap_buy_walks_multiple_levels(self) -> None:
        b = LiveBook(token_id="t")
        b.apply_snapshot(
            bids=[],
            asks=[(D("0.19"), D("40")), (D("0.20"), D("80")), (D("0.21"), D("120"))],
        )
        result = b.vwap_buy(D("100"))
        assert result is not None
        vwap, filled, levels = result
        assert vwap == D("0.196")  # (40*0.19 + 60*0.20) / 100
        assert filled == D("100")
        assert levels == 2

    def test_vwap_buy_partial_if_insufficient_depth(self) -> None:
        b = LiveBook(token_id="t")
        b.apply_snapshot(bids=[], asks=[(D("0.50"), D("10"))])
        result = b.vwap_buy(D("100"))
        assert result is not None
        _, filled, _ = result
        assert filled == D("10")

    def test_vwap_buy_none_on_empty_book(self) -> None:
        b = LiveBook(token_id="t")
        assert b.vwap_buy(D("10")) is None

    def test_to_snapshot_orders_bids_descending(self) -> None:
        b = LiveBook(token_id="t")
        b.apply_snapshot(
            bids=[(D("0.10"), D("1")), (D("0.12"), D("2")), (D("0.11"), D("3"))],
            asks=[],
        )
        snap = b.to_snapshot()
        assert [lvl.price for lvl in snap.bids] == [D("0.12"), D("0.11"), D("0.10")]

    def test_sequence_increments_on_updates(self) -> None:
        b = LiveBook(token_id="t")
        assert b.snapshots_applied == 0 and b.deltas_applied == 0
        b.apply_snapshot(bids=[], asks=[(D("0.5"), D("1"))])
        b.apply_delta([LevelChange(price=D("0.5"), size=D("2"), side=Side.ASK)])
        b.apply_delta([LevelChange(price=D("0.5"), size=D("0"), side=Side.ASK)])
        assert b.snapshots_applied == 1
        assert b.deltas_applied == 2


class TestBookRegistry:
    async def test_registry_publishes_updates_to_subscribers(self) -> None:
        reg = BookRegistry()
        events: list[str] = []

        async def reader() -> None:
            async for u in reg.updates():
                events.append(u.reason)
                if len(events) >= 2:
                    return

        task = asyncio.create_task(reader())
        await asyncio.sleep(0)
        reg.apply_snapshot("x", bids=[], asks=[(D("0.5"), D("1"))])
        reg.apply_delta("x", [LevelChange(price=D("0.5"), size=D("0"), side=Side.ASK)])
        await asyncio.wait_for(task, timeout=1.0)
        assert events == ["snapshot", "delta"]

    def test_get_returns_none_for_unknown_token(self) -> None:
        reg = BookRegistry()
        assert reg.get("missing") is None

    def test_book_creates_and_returns_same_instance(self) -> None:
        reg = BookRegistry()
        a = reg.book("t")
        b = reg.book("t")
        assert a is b


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BUY", Side.BID),
        ("buy", Side.BID),
        ("bid", Side.BID),
        ("SELL", Side.ASK),
        ("ASK", Side.ASK),
    ],
)
def test_level_change_from_raw(raw: str, expected: Side) -> None:
    lc = LevelChange.from_raw({"price": "0.5", "size": "1", "side": raw})
    assert lc.side is expected
