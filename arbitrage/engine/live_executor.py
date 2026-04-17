"""Live executor — deferred behind MODE=live.

Same `Executor` surface as PaperExecutor. Signs EIP-712 orders via
py-clob-client, submits FAK across all legs in parallel, aborts + unwinds on
partial fill, and calls NegRiskAdapter.redeemPositions once a complete set is
held.

Status: functional skeleton. The signing + order submission path is wired up,
but each side-effect is gated by a `dry_run` flag so nothing is broadcast until
the operator explicitly flips it. The risk gate is enforced here; it's the
last thing between an Opportunity and real capital.

See docs/api/order-signing.md and docs/api/negrisk.md for wire details.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from loguru import logger

from ..book.l2 import BookRegistry
from ..config import Mode, settings
from ..db import db_conn
from ..models import Basket, BasketStatus, Fill, Opportunity, OrderType, Side


@dataclass(slots=True)
class RiskLimits:
    max_basket_usd: Decimal
    max_open_baskets: int
    max_open_baskets_per_event: int
    daily_loss_stop_usd: Decimal
    kill_switch_file: Path

    @classmethod
    def from_settings(cls) -> RiskLimits:
        return cls(
            max_basket_usd=settings.max_basket_usd,
            max_open_baskets=settings.max_open_baskets,
            max_open_baskets_per_event=settings.max_open_baskets_per_event,
            daily_loss_stop_usd=settings.daily_loss_stop_usd,
            kill_switch_file=settings.kill_switch_file,
        )


class RiskDenied(Exception):
    """Raised when a risk gate refuses an opportunity."""


async def risk_gate(opp: Opportunity, limits: RiskLimits) -> None:
    """Apply hard caps. Raises RiskDenied with a reason if any cap is hit."""
    if limits.kill_switch_file.exists():
        raise RiskDenied(f"kill switch present: {limits.kill_switch_file}")

    cost = opp.sum_vwap_asks * opp.max_baskets
    if cost > limits.max_basket_usd:
        raise RiskDenied(f"basket cost ${cost} > max ${limits.max_basket_usd}")

    today = datetime.now(UTC).date().isoformat()
    async with db_conn() as conn:
        cursor = await conn.execute(
            """
            SELECT COUNT(*) FROM baskets
            WHERE is_paper=0 AND status IN (?, ?, ?)
            """,
            (
                BasketStatus.OPEN.value,
                BasketStatus.PARTIAL.value,
                BasketStatus.PENDING_RESOLUTION.value,
            ),
        )
        (open_global,) = await cursor.fetchone()
        if open_global >= limits.max_open_baskets:
            raise RiskDenied(f"{open_global} live baskets already open")

        cursor = await conn.execute(
            """
            SELECT COUNT(*) FROM baskets
            WHERE event_id=? AND is_paper=0 AND status IN (?, ?, ?)
            """,
            (
                opp.event_id,
                BasketStatus.OPEN.value,
                BasketStatus.PARTIAL.value,
                BasketStatus.PENDING_RESOLUTION.value,
            ),
        )
        (open_per_event,) = await cursor.fetchone()
        if open_per_event >= limits.max_open_baskets_per_event:
            raise RiskDenied(f"event {opp.event_id} already has {open_per_event} open")

        cursor = await conn.execute(
            "SELECT live_pnl_usd FROM daily_pnl WHERE date=?", (today,)
        )
        row = await cursor.fetchone()
        if row is not None:
            pnl = Decimal(row[0])
            if pnl <= -limits.daily_loss_stop_usd:
                raise RiskDenied(f"daily loss stop hit: pnl={pnl}")


class LiveExecutor:
    """Signs and submits orders. Requires MODE=live and wallet credentials."""

    def __init__(
        self,
        *,
        books: BookRegistry,
        limits: RiskLimits | None = None,
        dry_run: bool = True,
    ) -> None:
        if settings.mode != Mode.LIVE:
            raise RuntimeError(
                "LiveExecutor instantiated but ARB_MODE is not live — refusing."
            )
        settings.require_live_credentials()
        self._books = books
        self._limits = limits or RiskLimits.from_settings()
        self._dry_run = dry_run
        self._clob = None  # py_clob_client.ClobClient, lazy-init

    def _ensure_clob(self):
        if self._clob is not None:
            return self._clob
        # Imported lazily so paper-mode users don't need py-clob-client installed.
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        pk = settings.private_key.get_secret_value() if settings.private_key else None
        if pk is None:
            raise RuntimeError("ARB_PRIVATE_KEY is required for live mode")
        creds = None
        if settings.api_key and settings.api_secret and settings.api_passphrase:
            creds = ApiCreds(
                api_key=settings.api_key.get_secret_value(),
                api_secret=settings.api_secret.get_secret_value(),
                api_passphrase=settings.api_passphrase.get_secret_value(),
            )
        self._clob = ClobClient(
            host=settings.clob_host,
            key=pk,
            chain_id=137,
            signature_type=settings.signature_type,
            funder=settings.funder_address,
            creds=creds,
        )
        if creds is None:
            self._clob.set_api_creds(self._clob.create_or_derive_api_creds())
        return self._clob

    async def execute(self, opp: Opportunity) -> Basket | None:
        try:
            await risk_gate(opp, self._limits)
        except RiskDenied as exc:
            logger.warning("risk denied opp {}: {}", opp.id, exc)
            return None

        now = datetime.now(UTC)
        basket = Basket(
            opportunity_id=opp.id,
            event_id=opp.event_id,
            is_paper=False,
            created_at=now,
            basket_count=opp.max_baskets,
            total_cost_usd=Decimal(0),
            status=BasketStatus.OPEN,
            fills=[],
        )
        await self._persist_basket_open(opp, basket)

        fills, total_cost, shortfall = await self._submit_parallel(opp, basket)
        basket.fills = fills
        basket.total_cost_usd = total_cost

        if shortfall:
            logger.error("partial fill detected on basket {}; unwinding", basket.id)
            await self._unwind(basket, shortfall)
            basket.status = BasketStatus.FAILED
        else:
            basket.status = BasketStatus.PENDING_RESOLUTION
            await self._redeem_or_defer(basket, opp)

        await self._persist_basket_final(basket)
        return basket

    async def _submit_parallel(
        self, opp: Opportunity, basket: Basket
    ) -> tuple[list[Fill], Decimal, dict[str, Decimal]]:
        tasks = [self._submit_leg(opp, leg, basket.id) for leg in opp.legs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        fills: list[Fill] = []
        total_cost = Decimal(0)
        shortfall: dict[str, Decimal] = {}
        for leg, res in zip(opp.legs, results, strict=True):
            if isinstance(res, Exception):
                logger.error("leg {} raised: {}", leg.token_id, res)
                shortfall[leg.token_id] = leg.size
                continue
            fill, short = res
            fills.append(fill)
            total_cost += fill.price * fill.size
            if short > 0:
                shortfall[leg.token_id] = short
        return fills, total_cost, shortfall

    async def _submit_leg(self, opp: Opportunity, leg, basket_id: str):
        if self._dry_run:
            logger.info("[dry_run] would FAK buy token={} price={} size={}",
                        leg.token_id, leg.vwap_price, leg.size)
            fill = Fill(
                token_id=leg.token_id, side=Side.BUY,
                price=leg.vwap_price, size=leg.size,
                fee_usd=Decimal(0), filled_at=datetime.now(UTC),
            )
            return fill, Decimal(0)

        client = self._ensure_clob()
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.clob_types import OrderType as ClobOrderType

        args = OrderArgs(
            token_id=leg.token_id,
            price=float(leg.vwap_price),
            size=float(leg.size),
            side="BUY",
        )
        # neg_risk=True is critical — routes to NegRiskCtfExchange.
        signed = await asyncio.to_thread(
            client.create_order, args, options={"neg_risk": True}
        )
        resp = await asyncio.to_thread(
            client.post_order, signed, ClobOrderType.FAK
        )
        await self._persist_live_order(basket_id, leg, resp)
        filled_size = Decimal(str(resp.get("making_amount") or resp.get("size_matched") or 0))
        price = Decimal(str(resp.get("price") or leg.vwap_price))
        short = leg.size - filled_size
        fill = Fill(
            token_id=leg.token_id, side=Side.BUY,
            price=price, size=filled_size,
            fee_usd=Decimal(str(resp.get("fee") or 0)),
            filled_at=datetime.now(UTC),
        )
        return fill, max(short, Decimal(0))

    async def _unwind(self, basket: Basket, shortfall: dict[str, Decimal]) -> None:
        """Sell any legs we over-filled relative to the shortfalled ones."""
        short_legs = set(shortfall.keys())
        for fill in basket.fills:
            if fill.token_id in short_legs or fill.size <= 0:
                continue
            if self._dry_run:
                logger.info("[dry_run] would market-sell token={} size={}",
                            fill.token_id, fill.size)
                continue
            client = self._ensure_clob()
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.clob_types import OrderType as ClobOrderType
            args = OrderArgs(
                token_id=fill.token_id,
                price=0.0,  # market
                size=float(fill.size),
                side="SELL",
            )
            signed = await asyncio.to_thread(
                client.create_order, args, options={"neg_risk": True}
            )
            await asyncio.to_thread(client.post_order, signed, ClobOrderType.FAK)

    async def _redeem_or_defer(self, basket: Basket, opp: Opportunity) -> None:
        """Once the full YES set is held, call NegRiskAdapter.redeemPositions.
        Deferred (no-op) in MVP — the patient path is to wait for UMA and
        call redeem from a separate resolution worker. This keeps the hot
        path small and avoids gas on every successful basket.
        """
        logger.info("basket {} pending resolution; redeem deferred to watcher", basket.id)

    async def _persist_basket_open(self, opp: Opportunity, basket: Basket) -> None:
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
                    opp.id, opp.detected_at.isoformat(), opp.event_id, opp.event_title,
                    str(opp.sum_vwap_asks), opp.net_edge_bps, str(opp.max_baskets),
                    str(opp.expected_profit_usd), legs_json,
                ),
            )
            await conn.execute(
                """
                INSERT INTO baskets
                  (id, opportunity_id, event_id, is_paper, created_at, basket_count,
                   total_cost_usd, status)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (basket.id, basket.opportunity_id, basket.event_id,
                 basket.created_at.isoformat(), str(basket.basket_count),
                 str(basket.total_cost_usd), basket.status.value),
            )
            await conn.commit()

    async def _persist_basket_final(self, basket: Basket) -> None:
        async with db_conn() as conn:
            await conn.execute(
                """
                UPDATE baskets SET total_cost_usd=?, status=? WHERE id=?
                """,
                (str(basket.total_cost_usd), basket.status.value, basket.id),
            )
            for fill in basket.fills:
                await conn.execute(
                    """
                    INSERT INTO fills
                      (basket_id, token_id, side, price, size, fee_usd, filled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (basket.id, fill.token_id, fill.side.value, str(fill.price),
                     str(fill.size), str(fill.fee_usd), fill.filled_at.isoformat()),
                )
            await conn.commit()

    async def _persist_live_order(self, basket_id: str, leg, resp: dict) -> None:
        now = datetime.now(UTC).isoformat()
        async with db_conn() as conn:
            await conn.execute(
                """
                INSERT INTO live_orders
                  (id, basket_id, token_id, side, price, size, order_type, status,
                   clob_order_id, tx_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(resp.get("orderId") or resp.get("id") or f"{basket_id}-{leg.token_id}"),
                    basket_id, leg.token_id, Side.BUY.value,
                    str(leg.vwap_price), str(leg.size),
                    OrderType.FAK.value,
                    str(resp.get("status") or "submitted"),
                    resp.get("orderId") or resp.get("id"),
                    resp.get("transactionHash"),
                    now, now,
                ),
            )
            await conn.commit()
