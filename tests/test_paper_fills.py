"""Paper executor + resolution tests."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from arbitrage.book.l2 import BookRegistry
from arbitrage.db import db_conn
from arbitrage.engine.opportunity import EngineConfig, EventIndex, OpportunityEngine
from arbitrage.engine.paper_fills import (
    PaperExecutor,
    mark_resolution,
    simulate_leg_fill,
)
from arbitrage.models import BasketStatus, Event, Outcome


def _setup_event_and_books():
    ev = Event(
        id="evt",
        slug="s",
        title="t",
        is_neg_risk=True,
        end_date=None,
        outcomes=(
            Outcome(token_id="A", name="A", outcome_index=0),
            Outcome(token_id="B", name="B", outcome_index=1),
        ),
    )
    idx = EventIndex()
    idx.upsert(ev)
    reg = BookRegistry()
    reg.apply_snapshot("A", bids=[], asks=[(D("0.40"), D("200"))])
    reg.apply_snapshot("B", bids=[], asks=[(D("0.50"), D("200"))])
    eng = OpportunityEngine(
        books=reg,
        index=idx,
        config=EngineConfig(
            min_net_edge_bps=50,
            fees_per_share_usd=D("0"),
            gas_per_basket_usd=D("0.10"),
            max_basket_usd=D("100"),
        ),
    )
    return ev, reg, eng


def test_simulate_leg_fill_partial_depth() -> None:
    from arbitrage.book.l2 import LiveBook

    b = LiveBook(token_id="t")
    b.apply_snapshot(bids=[], asks=[(D("0.50"), D("5"))])
    res = simulate_leg_fill(b, D("100"))
    assert res.filled == D("5")
    assert res.vwap_price == D("0.50")


def test_simulate_leg_fill_empty_book() -> None:
    from arbitrage.book.l2 import LiveBook

    b = LiveBook(token_id="t")
    res = simulate_leg_fill(b, D("10"))
    assert res.filled == 0
    assert res.levels_consumed == 0


@pytest.mark.asyncio
async def test_successful_basket_reaches_pending_resolution(db) -> None:
    ev, reg, eng = _setup_event_and_books()
    opp = eng.evaluate(ev)
    assert opp is not None
    execr = PaperExecutor(books=reg, latency_ms=0)
    basket = await execr.execute_now(opp)
    assert basket is not None
    assert basket.status is BasketStatus.PENDING_RESOLUTION
    assert basket.basket_count == opp.max_baskets
    assert basket.total_cost_usd > 0


@pytest.mark.asyncio
async def test_vanishing_depth_produces_failed_basket(db) -> None:
    ev, reg, eng = _setup_event_and_books()
    opp = eng.evaluate(ev)
    assert opp is not None
    reg.apply_snapshot("A", bids=[], asks=[(D("0.40"), D("1"))])
    execr = PaperExecutor(books=reg, latency_ms=0)
    basket = await execr.execute_now(opp)
    assert basket is not None
    assert basket.status is BasketStatus.FAILED


@pytest.mark.asyncio
async def test_resolution_redeems_winning_basket(db) -> None:
    ev, reg, eng = _setup_event_and_books()
    opp = eng.evaluate(ev)
    assert opp is not None
    execr = PaperExecutor(books=reg, latency_ms=0)
    basket = await execr.execute_now(opp)
    assert basket is not None

    updated = await mark_resolution("evt", winning_token_id="A")
    assert updated == 1
    async with db_conn() as conn:
        row = await (
            await conn.execute(
                "SELECT status, realized_pnl_usd FROM baskets WHERE id=?", (basket.id,)
            )
        ).fetchone()
    assert row[0] == BasketStatus.REDEEMED.value
    assert D(row[1]) > 0


@pytest.mark.asyncio
async def test_invalid_resolution_marks_basket_loss(db) -> None:
    ev, reg, eng = _setup_event_and_books()
    opp = eng.evaluate(ev)
    assert opp is not None
    execr = PaperExecutor(books=reg, latency_ms=0)
    basket = await execr.execute_now(opp)
    assert basket is not None

    updated = await mark_resolution("evt", winning_token_id=None)
    assert updated == 1
    async with db_conn() as conn:
        row = await (
            await conn.execute(
                "SELECT status, realized_pnl_usd FROM baskets WHERE id=?", (basket.id,)
            )
        ).fetchone()
    assert row[0] == BasketStatus.INVALID.value
    assert D(row[1]) < 0


@pytest.mark.asyncio
async def test_resolution_is_idempotent(db) -> None:
    ev, reg, eng = _setup_event_and_books()
    opp = eng.evaluate(ev)
    assert opp is not None
    await PaperExecutor(books=reg, latency_ms=0).execute_now(opp)
    first = await mark_resolution("evt", winning_token_id="A")
    second = await mark_resolution("evt", winning_token_id="A")
    assert first == 1
    assert second == 0  # nothing pending anymore
