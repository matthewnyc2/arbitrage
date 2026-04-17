"""Scan loop: book updates -> engine.evaluate -> executor.execute.

One small glue function so the CLI and tests can both spin up the full
pipeline. Keeps the engine/executor decoupled — either side is swappable.
"""
from __future__ import annotations

import asyncio

from loguru import logger

from ..book.l2 import BookRegistry
from ..db import db_conn
from .executor import Executor
from .opportunity import EventIndex, OpportunityEngine


async def hydrate_event_index(index: EventIndex) -> int:
    """Load all active negRisk events from SQLite into the in-memory index."""
    from ..models import Event, Outcome

    async with db_conn() as conn:
        cur = await conn.execute(
            """
            SELECT id, slug, title, is_neg_risk, end_date
            FROM events
            WHERE active=1
            """
        )
        event_rows = await cur.fetchall()
        count = 0
        for row in event_rows:
            cur = await conn.execute(
                """
                SELECT token_id, name, outcome_index
                FROM outcomes WHERE event_id=? ORDER BY outcome_index
                """,
                (row[0],),
            )
            outs = await cur.fetchall()
            if len(outs) < 2:
                continue
            from datetime import datetime

            end_date = None
            if row[4]:
                try:
                    end_date = datetime.fromisoformat(row[4])
                except ValueError:
                    end_date = None
            ev = Event(
                id=row[0],
                slug=row[1],
                title=row[2],
                is_neg_risk=bool(row[3]),
                end_date=end_date,
                outcomes=tuple(
                    Outcome(token_id=o[0], name=o[1], outcome_index=o[2]) for o in outs
                ),
            )
            index.upsert(ev)
            count += 1
    return count


async def run_scan_loop(
    *,
    books: BookRegistry,
    index: EventIndex,
    engine: OpportunityEngine,
    executor: Executor,
) -> None:
    """Drive engine + executor off the book registry's update stream."""
    engine_task = asyncio.create_task(engine.run(), name="engine.run")
    logger.info("scan loop started ({} events hydrated)", len(index.by_event_id))
    try:
        async for opp in engine.opportunities():
            try:
                await executor.execute(opp)
            except Exception as exc:
                logger.exception("executor failed on opp {}: {}", opp.id, exc)
    finally:
        engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass
