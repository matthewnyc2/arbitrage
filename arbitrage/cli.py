"""Arbitrage CLI — `arb <subcommand>`.

Subcommands:
  arb init          Create the SQLite schema.
  arb discover      One pass of Gamma REST discovery (or --loop for forever).
  arb scan          Full paper loop: WS + engine + paper executor.
  arb web           Start the FastAPI + HTMX dashboard.
  arb resolve       Mark a resolution (manual fallback).
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from .config import Mode, settings
from .logging_setup import configure_logging


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arb", description="Polymarket arbitrage")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create SQLite schema")

    disc = sub.add_parser("discover", help="one pass of Gamma REST discovery")
    disc.add_argument("--loop", action="store_true", help="run forever")
    disc.add_argument("--interval", type=int, default=120, help="seconds between passes")
    disc.add_argument("--max-pages", type=int, default=20)

    sub.add_parser("scan", help="run the paper scan loop (WS + engine + executor)")

    webp = sub.add_parser("web", help="start the dashboard")
    webp.add_argument("--host", default=settings.web_host)
    webp.add_argument("--port", type=int, default=settings.web_port)

    res = sub.add_parser("resolve", help="mark an event as resolved")
    res.add_argument("event_id")
    res.add_argument(
        "--winner",
        default=None,
        help="winning token_id (omit or set to 'invalid' for an invalid resolution)",
    )
    res.add_argument("--source", default="manual")

    return p


async def _cmd_init() -> None:
    from .db import init_db

    await init_db()
    print(f"initialized {settings.db_path}")


async def _cmd_discover(loop: bool, interval: int, max_pages: int) -> None:
    from .clients.polymarket_rest import discover_once, discovery_loop

    if loop:
        await discovery_loop(interval_seconds=interval)
    else:
        stats = await discover_once(max_pages=max_pages)
        print(
            f"seen={stats.events_seen} negRisk={stats.neg_risk_events} "
            f"upserted={stats.upserted} malformed={stats.skipped_malformed}"
        )


async def _cmd_scan() -> None:
    from .book.l2 import BookRegistry
    from .clients.polymarket_ws import run_market_channels
    from .engine.live_executor import LiveExecutor
    from .engine.loop import hydrate_event_index, run_scan_loop
    from .engine.opportunity import EventIndex, OpportunityEngine
    from .engine.paper_fills import PaperExecutor

    books = BookRegistry()
    index = EventIndex()
    hydrated = await hydrate_event_index(index)
    if hydrated == 0:
        print("no events in DB; run `arb discover` first")
        return
    token_ids = list(index.by_token_id.keys())

    engine = OpportunityEngine(books=books, index=index)
    executor = (
        LiveExecutor(books=books, dry_run=False)
        if settings.mode == Mode.LIVE
        else PaperExecutor(books=books)
    )
    ws_task = asyncio.create_task(
        run_market_channels(token_ids, registry=books), name="ws"
    )
    try:
        await run_scan_loop(books=books, index=index, engine=engine, executor=executor)
    finally:
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass


def _cmd_web(host: str, port: int) -> None:
    import uvicorn

    uvicorn.run("arbitrage.web.app:app", host=host, port=port, reload=False)


async def _cmd_resolve(event_id: str, winner: str | None, source: str) -> None:
    from .engine.paper_fills import mark_resolution

    winning = None if (winner is None or winner.lower() == "invalid") else winner
    updated = await mark_resolution(event_id, winning_token_id=winning, source=source)
    print(f"resolved event {event_id}: {updated} basket(s) updated")


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)

    if args.cmd == "init":
        asyncio.run(_cmd_init())
    elif args.cmd == "discover":
        asyncio.run(_cmd_discover(args.loop, args.interval, args.max_pages))
    elif args.cmd == "scan":
        asyncio.run(_cmd_scan())
    elif args.cmd == "web":
        _cmd_web(args.host, args.port)
    elif args.cmd == "resolve":
        asyncio.run(_cmd_resolve(args.event_id, args.winner, args.source))
    else:  # argparse guarantees required=True; belt-and-braces
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
