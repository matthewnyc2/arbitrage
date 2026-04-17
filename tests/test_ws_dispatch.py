"""Polymarket CLOB WS message dispatch (parse-only, no live socket)."""
from __future__ import annotations

from decimal import Decimal as D

from arbitrage.book.l2 import BookRegistry
from arbitrage.clients.polymarket_ws import MarketChannel, shard_tokens


def _channel(reg: BookRegistry) -> MarketChannel:
    return MarketChannel(["tok1", "tok2"], registry=reg)


def test_book_snapshot_message_populates_registry() -> None:
    reg = BookRegistry()
    _channel(reg)._dispatch(
        {
            "event_type": "book",
            "asset_id": "tok1",
            "market": "m1",
            "bids": [{"price": "0.10", "size": "100"}, {"price": "0.12", "size": "50"}],
            "asks": [{"price": "0.20", "size": "80"}],
            "timestamp": "1700000000000",
            "hash": "0xaa",
        }
    )
    book = reg.get("tok1")
    assert book is not None
    assert book.best_bid() == (D("0.12"), D("50"))
    assert book.best_ask() == (D("0.20"), D("80"))
    assert book.last_hash == "0xaa"


def test_price_change_applies_delta_per_asset() -> None:
    reg = BookRegistry()
    ch = _channel(reg)
    ch._dispatch(
        {
            "event_type": "book",
            "asset_id": "tok1",
            "market": "m",
            "bids": [],
            "asks": [{"price": "0.20", "size": "80"}],
            "timestamp": "1",
        }
    )
    ch._dispatch(
        {
            "event_type": "price_change",
            "market": "m",
            "timestamp": "2",
            "price_changes": [
                {"asset_id": "tok1", "price": "0.20", "size": "0", "side": "SELL"},
                {"asset_id": "tok1", "price": "0.19", "size": "30", "side": "SELL"},
                {"asset_id": "tok1", "price": "0.11", "size": "60", "side": "BUY"},
            ],
        }
    )
    book = reg.get("tok1")
    assert book.best_ask() == (D("0.19"), D("30"))
    assert book.best_bid() == (D("0.11"), D("60"))


def test_zero_size_removes_level() -> None:
    reg = BookRegistry()
    ch = _channel(reg)
    ch._dispatch(
        {
            "event_type": "book",
            "asset_id": "tok1",
            "bids": [],
            "asks": [{"price": "0.20", "size": "10"}],
            "timestamp": "1",
        }
    )
    ch._dispatch(
        {
            "event_type": "price_change",
            "timestamp": "2",
            "price_changes": [
                {"asset_id": "tok1", "price": "0.20", "size": "0", "side": "SELL"}
            ],
        }
    )
    assert reg.get("tok1").best_ask() is None


def test_unknown_event_type_is_ignored() -> None:
    reg = BookRegistry()
    _channel(reg)._dispatch({"event_type": "who_knows", "asset_id": "tok1"})
    assert reg.get("tok1") is None


def test_shard_tokens_splits_evenly() -> None:
    shards = shard_tokens([str(i) for i in range(250)], shard_size=100)
    assert [len(s) for s in shards] == [100, 100, 50]
