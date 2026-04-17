from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _uuid() -> str:
    return uuid4().hex


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    GTC = "GTC"
    FAK = "FAK"
    FOK = "FOK"
    GTD = "GTD"


class BasketStatus(str, Enum):
    OPEN = "open"
    PARTIAL = "partial"
    FAILED = "failed"
    PENDING_RESOLUTION = "pending_resolution"
    REDEEMED = "redeemed"
    INVALID = "invalid"


class Outcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: str
    name: str
    outcome_index: int


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    slug: str
    title: str
    is_neg_risk: bool
    end_date: datetime | None
    outcomes: tuple[Outcome, ...]

    @property
    def n_outcomes(self) -> int:
        return len(self.outcomes)


class BookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal
    size: Decimal


class Book(BaseModel):
    """L2 order book for a single token. Bids descending, asks ascending."""

    token_id: str
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    sequence: int = 0
    updated_at: datetime

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    def vwap_buy(self, target_size: Decimal) -> tuple[Decimal, Decimal] | None:
        """Walk asks to fill `target_size`. Returns (vwap_price, filled_size).
        If the book can't fill the full size, returns the partial fill."""
        if target_size <= 0 or not self.asks:
            return None
        remaining = target_size
        cost = Decimal(0)
        filled = Decimal(0)
        for level in self.asks:
            take = min(remaining, level.size)
            cost += take * level.price
            filled += take
            remaining -= take
            if remaining <= 0:
                break
        if filled <= 0:
            return None
        return (cost / filled, filled)


class OpportunityLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: str
    outcome_name: str
    outcome_index: int
    vwap_price: Decimal
    size: Decimal
    levels_consumed: int


class Opportunity(BaseModel):
    """A snapshot of edge math for one event at one moment."""

    id: str = Field(default_factory=_uuid)
    detected_at: datetime
    event_id: str
    event_title: str
    legs: tuple[OpportunityLeg, ...]
    sum_vwap_asks: Decimal
    fees_per_share: Decimal
    gas_per_basket_usd: Decimal
    gross_edge_usd_per_basket: Decimal
    net_edge_usd_per_basket: Decimal
    net_edge_bps: int
    max_baskets: Decimal
    expected_profit_usd: Decimal

    @classmethod
    def from_legs(
        cls,
        *,
        detected_at: datetime,
        event: Event,
        legs: tuple[OpportunityLeg, ...],
        fees_per_share: Decimal,
        gas_per_basket_usd: Decimal,
        max_baskets: Decimal,
    ) -> Self:
        sum_asks = sum((leg.vwap_price for leg in legs), Decimal(0))
        gross_per_basket = Decimal(1) - sum_asks
        n = Decimal(event.n_outcomes)
        net_per_basket = gross_per_basket - (fees_per_share * n) - gas_per_basket_usd
        net_bps = int((net_per_basket / Decimal(1)) * Decimal(10_000)) if net_per_basket else 0
        expected = net_per_basket * max_baskets
        return cls(
            detected_at=detected_at,
            event_id=event.id,
            event_title=event.title,
            legs=legs,
            sum_vwap_asks=sum_asks,
            fees_per_share=fees_per_share,
            gas_per_basket_usd=gas_per_basket_usd,
            gross_edge_usd_per_basket=gross_per_basket,
            net_edge_usd_per_basket=net_per_basket,
            net_edge_bps=net_bps,
            max_baskets=max_baskets,
            expected_profit_usd=expected,
        )


class Fill(BaseModel):
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    fee_usd: Decimal
    filled_at: datetime


class Basket(BaseModel):
    id: str = Field(default_factory=_uuid)
    opportunity_id: str
    event_id: str
    is_paper: bool
    created_at: datetime
    basket_count: Decimal
    total_cost_usd: Decimal
    status: BasketStatus
    fills: list[Fill] = Field(default_factory=list)
    redeemed_at: datetime | None = None
    redeemed_payout_usd: Decimal | None = None
    realized_pnl_usd: Decimal | None = None


class Resolution(BaseModel):
    event_id: str
    winning_outcome_token_id: str | None
    resolved_at: datetime
    source: str
