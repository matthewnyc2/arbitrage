"""FastAPI + HTMX dashboard.

Single page. Server-rendered via Jinja. HTMX polls the fragment endpoints
every few seconds so there's no client-side state and no build step.
Endpoints:

  GET  /              full page
  GET  /fragments/opportunities  table of recent opportunities
  GET  /fragments/baskets        table of open + recent baskets
  GET  /fragments/pnl            paper pnl summary + mode indicator
  POST /kill                     touch the kill switch file (immediate halt)
  POST /unkill                   remove the kill switch file
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..db import db_conn, init_db

TEMPLATE_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="arbitrage dashboard", lifespan=_lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    # Python 3.14 + Jinja2 LRUCache regression: disable caching.
    templates.env.cache = None

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "index.html", {"mode": settings.mode.value}
        )

    @app.get("/fragments/opportunities", response_class=HTMLResponse)
    async def opps(request: Request) -> HTMLResponse:
        rows = await _recent_opportunities(limit=25)
        return templates.TemplateResponse(
            request, "fragments/opportunities.html", {"rows": rows}
        )

    @app.get("/fragments/baskets", response_class=HTMLResponse)
    async def baskets(request: Request) -> HTMLResponse:
        rows = await _recent_baskets(limit=25)
        return templates.TemplateResponse(
            request, "fragments/baskets.html", {"rows": rows}
        )

    @app.get("/fragments/pnl", response_class=HTMLResponse)
    async def pnl(request: Request) -> HTMLResponse:
        summary = await _paper_pnl_summary()
        kill_active = settings.kill_switch_file.exists()
        return templates.TemplateResponse(
            request,
            "fragments/pnl.html",
            {
                "mode": settings.mode.value,
                "kill_active": kill_active,
                **summary,
            },
        )

    @app.post("/kill", response_class=HTMLResponse)
    async def kill(request: Request) -> HTMLResponse:
        settings.kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
        settings.kill_switch_file.touch(exist_ok=True)
        return await pnl(request)

    @app.post("/unkill", response_class=HTMLResponse)
    async def unkill(request: Request) -> HTMLResponse:
        path = settings.kill_switch_file
        if path.exists():
            path.unlink()
        return await pnl(request)

    return app


async def _recent_opportunities(limit: int) -> list[dict]:
    async with db_conn() as conn:
        cur = await conn.execute(
            """
            SELECT detected_at, event_title, sum_vwap_asks, net_edge_bps,
                   max_baskets, expected_profit_usd, acted_on
            FROM opportunities
            ORDER BY detected_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    return [
        {
            "detected_at": r[0],
            "event_title": r[1],
            "sum_vwap_asks": r[2],
            "net_edge_bps": r[3],
            "max_baskets": r[4],
            "expected_profit_usd": r[5],
            "acted_on": bool(r[6]),
        }
        for r in rows
    ]


async def _recent_baskets(limit: int) -> list[dict]:
    async with db_conn() as conn:
        cur = await conn.execute(
            """
            SELECT b.id, b.created_at, e.title, b.is_paper, b.basket_count,
                   b.total_cost_usd, b.status, b.realized_pnl_usd
            FROM baskets b
            LEFT JOIN events e ON e.id = b.event_id
            ORDER BY b.created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r[0][:8],
            "created_at": r[1],
            "event_title": r[2] or "?",
            "is_paper": bool(r[3]),
            "basket_count": r[4],
            "total_cost_usd": r[5],
            "status": r[6],
            "realized_pnl_usd": r[7],
        }
        for r in rows
    ]


async def _paper_pnl_summary() -> dict:
    async with db_conn() as conn:
        cur = await conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status='redeemed' THEN 1 ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN status='pending_resolution' THEN 1 ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN status='invalid' THEN 1 ELSE 0 END), 0)
            FROM baskets WHERE is_paper=1
            """
        )
        counts = await cur.fetchone()
        cur = await conn.execute(
            "SELECT realized_pnl_usd FROM baskets "
            "WHERE is_paper=1 AND realized_pnl_usd IS NOT NULL"
        )
        pnl_rows = await cur.fetchall()
    total = Decimal(0)
    for (val,) in pnl_rows:
        if val is None:
            continue
        try:
            total += Decimal(val)
        except (ArithmeticError, ValueError):
            pass
    return {
        "redeemed": counts[0],
        "pending": counts[1],
        "failed": counts[2],
        "invalid": counts[3],
        "realized_pnl_usd": f"{total:.4f}",
    }


app = create_app()
