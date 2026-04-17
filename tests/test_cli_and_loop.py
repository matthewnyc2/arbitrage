"""CLI + scan-loop integration tests."""
from __future__ import annotations

import asyncio
from decimal import Decimal as D

import pytest

from arbitrage.book.l2 import BookRegistry
from arbitrage.clients.polymarket_rest import normalize_event, upsert_events
from arbitrage.engine.loop import hydrate_event_index, run_scan_loop
from arbitrage.engine.opportunity import EngineConfig, EventIndex, OpportunityEngine
from arbitrage.engine.paper_fills import PaperExecutor
from arbitrage.models import BasketStatus


def _raw_event(event_id: str = "0xmkt") -> dict:
    return {
        "id": event_id,
        "slug": "slug",
        "title": "Title",
        "negRisk": True,
        "negRiskMarketID": event_id,
        "markets": [
            {"clobTokenIds": ["A", "A2"], "groupItemTitle": "A"},
            {"clobTokenIds": ["B", "B2"], "groupItemTitle": "B"},
        ],
    }


@pytest.mark.asyncio
async def test_hydrate_event_index_loads_from_db(db) -> None:
    ev = normalize_event(_raw_event())
    assert ev is not None
    await upsert_events([ev])
    index = EventIndex()
    n = await hydrate_event_index(index)
    assert n == 1
    assert index.event_for_token("A") is not None


@pytest.mark.asyncio
async def test_run_scan_loop_emits_basket_from_live_book(db) -> None:
    ev = normalize_event(_raw_event())
    assert ev is not None
    await upsert_events([ev])
    index = EventIndex()
    await hydrate_event_index(index)

    books = BookRegistry()
    engine = OpportunityEngine(
        books=books,
        index=index,
        config=EngineConfig(
            min_net_edge_bps=50,
            fees_per_share_usd=D("0"),
            gas_per_basket_usd=D("0.10"),
            max_basket_usd=D("50"),
        ),
    )
    executor = PaperExecutor(books=books, latency_ms=0)

    # Kick the scan loop off and feed it two arbitrage-crossing books.
    task = asyncio.create_task(
        run_scan_loop(books=books, index=index, engine=engine, executor=executor)
    )
    # Give both the scan loop and engine.run() a chance to subscribe.
    for _ in range(5):
        await asyncio.sleep(0)
    books.apply_snapshot("A", bids=[], asks=[(D("0.40"), D("200"))])
    books.apply_snapshot("B", bids=[], asks=[(D("0.50"), D("200"))])

    # Wait for a basket row to appear (bounded)
    from arbitrage.db import db_conn

    async def has_basket() -> bool:
        async with db_conn() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM baskets")
            (n,) = await cur.fetchone()
            return n > 0

    for _ in range(40):  # up to ~2s
        if await has_basket():
            break
        await asyncio.sleep(0.05)
    assert await has_basket(), "scan loop did not persist a basket"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_cli_help_exits_cleanly() -> None:
    from arbitrage.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_cli_init_creates_db(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("ARB_DB_PATH", str(db_path))
    import arbitrage.config as cfg
    cfg.settings = cfg.Settings()
    import arbitrage.db as dbmod
    dbmod.settings = cfg.settings
    import arbitrage.cli as cli
    cli.settings = cfg.settings

    cli.main(["init"])
    assert db_path.exists()


def test_cli_resolve_updates_basket(_tmp_arb_env) -> None:
    """End-to-end: set up a paper basket then run `arb resolve` to redeem it."""
    async def setup() -> str:
        from arbitrage.db import init_db
        await init_db()
        ev = normalize_event(_raw_event())
        assert ev is not None
        await upsert_events([ev])
        index = EventIndex()
        await hydrate_event_index(index)

        books = BookRegistry()
        books.apply_snapshot("A", bids=[], asks=[(D("0.40"), D("200"))])
        books.apply_snapshot("B", bids=[], asks=[(D("0.50"), D("200"))])
        engine = OpportunityEngine(
            books=books, index=index,
            config=EngineConfig(
                min_net_edge_bps=50, fees_per_share_usd=D("0"),
                gas_per_basket_usd=D("0.10"), max_basket_usd=D("50"),
            ),
        )
        opp = engine.evaluate(index.by_event_id["0xmkt"])
        assert opp is not None
        basket = await PaperExecutor(books=books, latency_ms=0).execute_now(opp)
        assert basket is not None
        return basket.id

    async def check(basket_id: str) -> str:
        from arbitrage.db import db_conn
        async with db_conn() as conn:
            (status,) = await (
                await conn.execute(
                    "SELECT status FROM baskets WHERE id=?", (basket_id,)
                )
            ).fetchone()
        return status

    basket_id = asyncio.run(setup())
    from arbitrage.cli import main
    rc = main(["resolve", "0xmkt", "--winner", "A"])
    assert rc == 0
    assert asyncio.run(check(basket_id)) == BasketStatus.REDEEMED.value
