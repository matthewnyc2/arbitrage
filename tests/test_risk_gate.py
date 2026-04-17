"""Live executor risk gate tests — no py-clob-client required."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from arbitrage.db import db_conn
from arbitrage.engine.live_executor import RiskDenied, RiskLimits, risk_gate
from arbitrage.models import Event, Opportunity, OpportunityLeg, Outcome


def _opportunity(cost_per_share: D, size: D) -> Opportunity:
    ev = Event(
        id="e",
        slug="e",
        title="t",
        is_neg_risk=True,
        end_date=None,
        outcomes=(
            Outcome(token_id="A", name="A", outcome_index=0),
            Outcome(token_id="B", name="B", outcome_index=1),
        ),
    )
    return Opportunity.from_legs(
        detected_at=datetime.now(UTC),
        event=ev,
        legs=(
            OpportunityLeg(
                token_id="A",
                outcome_name="A",
                outcome_index=0,
                vwap_price=cost_per_share / D(2),
                size=size,
                levels_consumed=1,
            ),
            OpportunityLeg(
                token_id="B",
                outcome_name="B",
                outcome_index=1,
                vwap_price=cost_per_share / D(2),
                size=size,
                levels_consumed=1,
            ),
        ),
        fees_per_share=D("0"),
        gas_per_basket_usd=D("0.10"),
        max_baskets=size,
    )


def _limits(**overrides) -> RiskLimits:
    base = dict(
        max_basket_usd=D("200"),
        max_open_baskets=3,
        max_open_baskets_per_event=1,
        daily_loss_stop_usd=D("100"),
        kill_switch_file=Path("/nope/does-not-exist"),
    )
    base.update(overrides)
    return RiskLimits(**base)


@pytest.mark.asyncio
async def test_risk_gate_accepts_within_limits(db) -> None:
    opp = _opportunity(D("0.90"), D("100"))  # cost = $90
    await risk_gate(opp, _limits())


@pytest.mark.asyncio
async def test_risk_gate_rejects_oversized_basket(db) -> None:
    opp = _opportunity(D("0.90"), D("1000"))  # cost = $900
    with pytest.raises(RiskDenied, match="basket cost"):
        await risk_gate(opp, _limits(max_basket_usd=D("500")))


@pytest.mark.asyncio
async def test_risk_gate_rejects_when_kill_switch_present(db, tmp_path) -> None:
    kill = tmp_path / "KILL"
    kill.touch()
    opp = _opportunity(D("0.90"), D("100"))
    with pytest.raises(RiskDenied, match="kill switch"):
        await risk_gate(opp, _limits(kill_switch_file=kill))


@pytest.mark.asyncio
async def test_risk_gate_rejects_daily_loss_stop(db) -> None:
    async with db_conn() as conn:
        today = datetime.now(UTC).date().isoformat()
        await conn.execute(
            "INSERT INTO daily_pnl (date, live_pnl_usd) VALUES (?, ?)",
            (today, "-150.00"),
        )
        await conn.commit()
    opp = _opportunity(D("0.90"), D("100"))
    with pytest.raises(RiskDenied, match="daily loss stop"):
        await risk_gate(opp, _limits(daily_loss_stop_usd=D("100")))
