"""REST discovery normalization + persistence tests."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from arbitrage.clients.polymarket_rest import (
    mark_inactive,
    normalize_event,
    upsert_events,
)
from arbitrage.db import db_conn


def _good_raw() -> dict:
    return {
        "id": 12345,
        "slug": "world-cup",
        "title": "World Cup",
        "negRisk": True,
        "negRiskMarketID": "0xmkt",
        "endDate": "2026-07-20T00:00:00Z",
        "markets": [
            {"conditionId": "0xc1", "clobTokenIds": ["11", "22"], "groupItemTitle": "Brazil"},
            {"conditionId": "0xc2", "clobTokenIds": '["33","44"]', "groupItemTitle": "France"},
            {"conditionId": "0xc3", "clobTokenIds": ["55", "66"], "groupItemTitle": "Argentina"},
        ],
    }


class TestNormalize:
    def test_accepts_well_formed_event(self) -> None:
        ev = normalize_event(_good_raw())
        assert ev is not None
        assert ev.id == "0xmkt"
        assert len(ev.outcomes) == 3
        assert [o.token_id for o in ev.outcomes] == ["11", "33", "55"]
        assert [o.name for o in ev.outcomes] == ["Brazil", "France", "Argentina"]
        assert ev.end_date is not None and ev.end_date.year == 2026

    def test_rejects_non_neg_risk(self) -> None:
        raw = _good_raw() | {"negRisk": False}
        assert normalize_event(raw) is None

    def test_rejects_too_few_outcomes(self) -> None:
        raw = _good_raw()
        raw["markets"] = raw["markets"][:1]
        assert normalize_event(raw) is None

    def test_rejects_closed_child_market(self) -> None:
        raw = _good_raw()
        raw["markets"][0]["closed"] = True
        assert normalize_event(raw) is None

    def test_rejects_duplicate_token_ids(self) -> None:
        raw = _good_raw()
        raw["markets"][1]["clobTokenIds"] = ["11", "99"]  # dup of market[0]
        assert normalize_event(raw) is None

    def test_parses_json_string_token_ids(self) -> None:
        raw = _good_raw()
        raw["markets"][0]["clobTokenIds"] = '["11","22"]'
        ev = normalize_event(raw)
        assert ev is not None
        assert ev.outcomes[0].token_id == "11"

    def test_rejects_malformed_token_ids_json(self) -> None:
        raw = _good_raw()
        raw["markets"][0]["clobTokenIds"] = "not-json{"
        assert normalize_event(raw) is None

    def test_falls_back_to_event_id_when_neg_risk_market_id_missing(self) -> None:
        raw = _good_raw()
        del raw["negRiskMarketID"]
        ev = normalize_event(raw)
        assert ev is not None
        assert ev.id == "12345"


@pytest.mark.asyncio
async def test_upsert_is_idempotent(db) -> None:
    ev = normalize_event(_good_raw())
    assert ev is not None
    assert await upsert_events([ev]) == 1
    assert await upsert_events([ev]) == 1
    async with db_conn() as conn:
        (count,) = await (await conn.execute("SELECT COUNT(*) FROM events")).fetchone()
        (outcome_count,) = await (
            await conn.execute("SELECT COUNT(*) FROM outcomes")
        ).fetchone()
    assert count == 1
    assert outcome_count == 3


@pytest.mark.asyncio
async def test_mark_inactive_flips_dropped_events(db) -> None:
    ev = normalize_event(_good_raw())
    assert ev is not None
    await upsert_events([ev])
    dropped = await mark_inactive({"kept-other-event"})
    assert dropped == 1
    async with db_conn() as conn:
        (active,) = await (
            await conn.execute("SELECT active FROM events WHERE id=?", (ev.id,))
        ).fetchone()
    assert active == 0


@pytest.mark.asyncio
async def test_mark_inactive_preserves_kept_events(db) -> None:
    ev = normalize_event(_good_raw())
    assert ev is not None
    await upsert_events([ev])
    dropped = await mark_inactive({ev.id})
    assert dropped == 0
    async with db_conn() as conn:
        (active,) = await (
            await conn.execute("SELECT active FROM events WHERE id=?", (ev.id,))
        ).fetchone()
    assert active == 1
