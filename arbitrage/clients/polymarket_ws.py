"""Polymarket CLOB market-channel WebSocket subscriber.

Wire format reference (April 2026):
  - endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
  - subscribe:   {"type":"market","assets_ids":[...],"custom_feature_enabled":true}
  - snapshot:    event_type="book", asks/bids are lists of {price,size} strings
  - delta:       event_type="price_change", wrapper="price_changes",
                 side="BUY"|"SELL", size="0" means remove
  - keepalive:   send the literal text frame "PING" every ~10s
  - desync:      no sequence numbers; on hash mismatch or missed heartbeat,
                 drop state and re-subscribe (server re-pushes snapshot)
  - sharding:    cap tokens/socket around 100-200; open more sockets as needed
"""
from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import orjson
import websockets
from loguru import logger
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from ..book.l2 import BookRegistry, LevelChange, Side
from ..config import settings

DEFAULT_SHARD_SIZE = 100
PING_INTERVAL_S = 10.0
RECONNECT_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 15.0)


def _to_decimal(v: object, default: Decimal = Decimal(0)) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _parse_ts_ms(v: Any) -> datetime:
    try:
        ms = int(v)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _parse_level_list(raw: Any) -> list[tuple[Decimal, Decimal]]:
    out: list[tuple[Decimal, Decimal]] = []
    if not isinstance(raw, list):
        return out
    for lvl in raw:
        if not isinstance(lvl, dict):
            continue
        price = _to_decimal(lvl.get("price"))
        size = _to_decimal(lvl.get("size"))
        if price <= 0:
            continue
        out.append((price, size))
    return out


def _side_from_buy_sell(raw: Any) -> Side | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip().upper()
    if s == "BUY":
        return Side.BID
    if s == "SELL":
        return Side.ASK
    return None


class MarketChannel:
    """Maintains a single WS connection for one shard of token ids."""

    def __init__(
        self,
        token_ids: list[str],
        *,
        registry: BookRegistry,
        url: str | None = None,
    ) -> None:
        if not token_ids:
            raise ValueError("MarketChannel needs at least one token id")
        self._token_ids = list(token_ids)
        self._registry = registry
        self._url = url or settings.ws_url
        self._stop = asyncio.Event()

    async def run(self) -> None:
        backoff_cycle = itertools.cycle(RECONNECT_BACKOFF_S)
        while not self._stop.is_set():
            try:
                await self._connect_and_consume()
                # Clean exit (e.g. stop requested); break loop.
                if self._stop.is_set():
                    return
                delay = 1.0
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                delay = next(backoff_cycle)
                logger.warning(
                    "ws disconnect ({}); reconnecting in {}s", exc.__class__.__name__, delay
                )
            except Exception as exc:
                delay = next(backoff_cycle)
                logger.exception("ws fatal ({}); reconnecting in {}s", exc, delay)
            await asyncio.sleep(delay)

    def stop(self) -> None:
        self._stop.set()

    async def _connect_and_consume(self) -> None:
        async with websockets.connect(self._url, max_size=2**22) as ws:
            await self._subscribe(ws)
            heartbeat = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    if self._stop.is_set():
                        break
                    self._handle_message(raw)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

    async def _subscribe(self, ws: ClientConnection) -> None:
        msg = orjson.dumps(
            {
                "type": "market",
                "assets_ids": self._token_ids,
                "custom_feature_enabled": True,
            }
        )
        await ws.send(msg)
        logger.info("ws subscribed to {} tokens", len(self._token_ids))

    async def _heartbeat(self, ws: ClientConnection) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(PING_INTERVAL_S)
            try:
                await ws.send("PING")
            except ConnectionClosed:
                return

    def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, str):
            if raw.strip() in ("PONG", "PING"):
                return
            payload = orjson.loads(raw)
        else:
            # Binary frames shouldn't normally arrive from this channel.
            try:
                payload = orjson.loads(raw)
            except orjson.JSONDecodeError:
                return
        if isinstance(payload, list):
            for item in payload:
                self._dispatch(item)
        elif isinstance(payload, dict):
            self._dispatch(payload)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("event_type")
        if event_type == "book":
            self._apply_book(msg)
        elif event_type == "price_change":
            self._apply_price_change(msg)
        elif event_type in ("tick_size_change", "last_trade_price", "best_bid_ask"):
            # Not used by the arb math today; logged at debug.
            logger.debug("ws {}: {}", event_type, msg)
        elif event_type in ("new_market", "market_resolved"):
            logger.info("ws {}: {}", event_type, msg)
        else:
            logger.debug("ws unknown event_type={}: {}", event_type, msg)

    def _apply_book(self, msg: dict[str, Any]) -> None:
        asset_id = msg.get("asset_id")
        if not isinstance(asset_id, str):
            return
        bids = _parse_level_list(msg.get("bids"))
        asks = _parse_level_list(msg.get("asks"))
        ts = _parse_ts_ms(msg.get("timestamp"))
        self._registry.apply_snapshot(
            asset_id, bids=bids, asks=asks, timestamp=ts, book_hash=msg.get("hash")
        )

    def _apply_price_change(self, msg: dict[str, Any]) -> None:
        ts = _parse_ts_ms(msg.get("timestamp"))
        changes_raw = msg.get("price_changes")
        if not isinstance(changes_raw, list):
            return
        by_asset: dict[str, list[LevelChange]] = {}
        latest_hash: dict[str, str] = {}
        for c in changes_raw:
            if not isinstance(c, dict):
                continue
            asset_id = c.get("asset_id")
            side = _side_from_buy_sell(c.get("side"))
            if not isinstance(asset_id, str) or side is None:
                continue
            price = _to_decimal(c.get("price"))
            size = _to_decimal(c.get("size"))
            if price <= 0:
                continue
            by_asset.setdefault(asset_id, []).append(
                LevelChange(price=price, size=size, side=side)
            )
            h = c.get("hash")
            if isinstance(h, str):
                latest_hash[asset_id] = h
        for asset_id, changes in by_asset.items():
            self._registry.apply_delta(
                asset_id,
                changes,
                timestamp=ts,
                book_hash=latest_hash.get(asset_id),
            )


def shard_tokens(token_ids: list[str], shard_size: int = DEFAULT_SHARD_SIZE) -> list[list[str]]:
    return [token_ids[i : i + shard_size] for i in range(0, len(token_ids), shard_size)]


async def run_market_channels(
    token_ids: list[str],
    *,
    registry: BookRegistry,
    shard_size: int = DEFAULT_SHARD_SIZE,
) -> None:
    """Run one MarketChannel per shard concurrently. Returns when all exit."""
    if not token_ids:
        return
    channels = [MarketChannel(shard, registry=registry) for shard in shard_tokens(token_ids, shard_size)]
    try:
        await asyncio.gather(*(ch.run() for ch in channels))
    finally:
        for ch in channels:
            ch.stop()
