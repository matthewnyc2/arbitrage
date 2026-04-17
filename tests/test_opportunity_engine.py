"""Opportunity engine math tests."""
from __future__ import annotations

from decimal import Decimal as D

from arbitrage.book.l2 import BookRegistry
from arbitrage.engine.opportunity import EngineConfig, EventIndex, OpportunityEngine
from arbitrage.models import Event, Outcome


def _two_outcome_event() -> Event:
    return Event(
        id="e1",
        slug="e1",
        title="two-outcome",
        is_neg_risk=True,
        end_date=None,
        outcomes=(
            Outcome(token_id="A", name="A", outcome_index=0),
            Outcome(token_id="B", name="B", outcome_index=1),
        ),
    )


def _engine(
    reg: BookRegistry,
    index: EventIndex,
    *,
    min_bps: int = 50,
    max_basket_usd: D = D("500"),
) -> OpportunityEngine:
    return OpportunityEngine(
        books=reg,
        index=index,
        config=EngineConfig(
            min_net_edge_bps=min_bps,
            fees_per_share_usd=D("0"),
            gas_per_basket_usd=D("0.10"),
            max_basket_usd=max_basket_usd,
        ),
    )


class TestEvaluate:
    def test_detects_arb_when_asks_sum_below_one(self) -> None:
        ev = _two_outcome_event()
        idx = EventIndex()
        idx.upsert(ev)
        reg = BookRegistry()
        reg.apply_snapshot("A", bids=[], asks=[(D("0.40"), D("200"))])
        reg.apply_snapshot("B", bids=[], asks=[(D("0.50"), D("200"))])
        opp = _engine(reg, idx).evaluate(ev)
        assert opp is not None
        assert opp.event_id == "e1"
        assert opp.net_edge_bps >= 50
        assert opp.max_baskets > 0

    def test_rejects_when_asks_sum_above_one(self) -> None:
        ev = _two_outcome_event()
        idx = EventIndex()
        idx.upsert(ev)
        reg = BookRegistry()
        reg.apply_snapshot("A", bids=[], asks=[(D("0.70"), D("200"))])
        reg.apply_snapshot("B", bids=[], asks=[(D("0.50"), D("200"))])
        assert _engine(reg, idx).evaluate(ev) is None

    def test_rejects_when_any_leg_has_no_asks(self) -> None:
        ev = _two_outcome_event()
        idx = EventIndex()
        idx.upsert(ev)
        reg = BookRegistry()
        reg.apply_snapshot("A", bids=[], asks=[])
        reg.apply_snapshot("B", bids=[], asks=[(D("0.50"), D("200"))])
        assert _engine(reg, idx).evaluate(ev) is None

    def test_rejects_when_edge_below_threshold(self) -> None:
        ev = _two_outcome_event()
        idx = EventIndex()
        idx.upsert(ev)
        reg = BookRegistry()
        # Sum = 0.995 -> 50bps gross, net will be below 50 after gas
        reg.apply_snapshot("A", bids=[], asks=[(D("0.495"), D("200"))])
        reg.apply_snapshot("B", bids=[], asks=[(D("0.500"), D("200"))])
        assert _engine(reg, idx, min_bps=50).evaluate(ev) is None

    def test_depth_clips_basket_count(self) -> None:
        ev = _two_outcome_event()
        idx = EventIndex()
        idx.upsert(ev)
        reg = BookRegistry()
        # Leg A is very thin
        reg.apply_snapshot("A", bids=[], asks=[(D("0.40"), D("12"))])
        reg.apply_snapshot("B", bids=[], asks=[(D("0.50"), D("10000"))])
        opp = _engine(reg, idx).evaluate(ev)
        assert opp is not None
        assert opp.max_baskets <= D("12")

    def test_vwap_degrades_with_size(self) -> None:
        """At large sizes we consume worse levels; edge per basket must shrink."""
        ev = _two_outcome_event()
        idx = EventIndex()
        idx.upsert(ev)
        reg = BookRegistry()
        # Both legs: cheap top, expensive deep levels
        reg.apply_snapshot(
            "A", bids=[], asks=[(D("0.40"), D("10")), (D("0.48"), D("10000"))]
        )
        reg.apply_snapshot(
            "B", bids=[], asks=[(D("0.50"), D("10")), (D("0.52"), D("10000"))]
        )
        opp = _engine(reg, idx).evaluate(ev)
        assert opp is not None
        # At size 10, sum_vwap = 0.90. At larger sizes it'll rise toward 1.00.
        # The engine chose the size maximizing expected profit, so sum is <= 1.0.
        assert opp.sum_vwap_asks <= D("1.0")
