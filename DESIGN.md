# Polymarket Arbitrage Bot — Design

## Goal

Detect and (eventually) capture **NegRisk multi-outcome arbitrage** on Polymarket: cases where the sum of best-ask prices across all outcomes of a categorical event is less than $1, allowing a complete-set buy + redeem for a guaranteed $1 payout.

Two modes from day one:

- **Paper** — runs end-to-end against live data, simulates fills, tracks PnL through real on-chain resolutions. No private keys. No capital at risk.
- **Live** — signs and submits real orders via `py-clob-client`, hard-capped by basket size, open-basket count, and daily loss.

A single env var (`ARB_MODE`) flips between them. The opportunity scanner, book maintainer, and risk gates are identical in both modes.

## The arbitrage

For a Polymarket negRisk event with outcomes O₁..Oₙ, each outcome trades as its own ERC-1155 token. If outcome Oᵢ wins, holders of Oᵢ receive $1 per share; all other outcomes pay $0. Exactly one outcome wins, so:

```
Σ fair_price(Oᵢ) = $1
```

When `Σ best_ask(Oᵢ) < $1`, buying one share of every outcome costs less than the guaranteed $1 payout. The basket is risk-free (subject to resolution risk — see below) and self-redeems via `NegRiskAdapter.redeemPositions`.

**Net edge per basket**:
```
net_edge = $1
        - Σ vwap_ask(Oᵢ, size)
        - Σ taker_fee(Oᵢ)
        - amortized_gas(per_basket)
        - safety_margin
```

## Architecture

```
        Gamma REST                 CLOB WebSocket
            │                            │
            ▼                            ▼
     Event/Outcome                  L2 Book Maintainer
       discovery                  (per token_id, in-mem)
            │                            │
            └────────────┬───────────────┘
                         ▼
                Opportunity Engine
       (Σ best-asks, depth-walk, fee-net, threshold)
                         │
                         ▼
                 Risk Gate (limits)
                         │
            ┌────────────┴───────────────┐
            ▼                            ▼
     Paper Executor              Live Executor
   (sim fills + PnL)        (sign + IOC + redeem)
            │                            │
            └────────────┬───────────────┘
                         ▼
                    SQLite (WAL)
                         │
                         ▼
            FastAPI + HTMX dashboard
```

## Why this stack

- **Python 3.12 + asyncio** — official `py-clob-client` is Python; mature `web3.py`; fewer moving parts than a JS toolchain.
- **FastAPI + HTMX + Jinja2** — single HTML page, no SPA build step, no client-side state.
- **SQLite (WAL)** — single-process app, single user; zero ops. Upgrade path to Postgres if multi-process ever happens.
- **pydantic v2** — typed contracts at every boundary.
- **loguru + structured JSON logs** — searchable post-hoc.
- **pytest + pytest-asyncio + respx** — book/math/sim layers fully unit-tested with mocked HTTP.

## Data model

| Entity | Key | Notes |
|---|---|---|
| `Event` | `id` | `negRiskMarketID` for negRisk events; `condition_id` for vanilla. Holds 2..N outcomes. |
| `Outcome` | `token_id` | ERC-1155 token id (CTF positionId). One per outcome per event. |
| `Book` | `(token_id)` in-mem | Sorted L2; replaced on snapshot, mutated on `price_change`. |
| `Opportunity` | `uuid` | Snapshot of legs + sizing + edge math. Persisted whether acted on or not. |
| `Basket` | `uuid` | A set of paper or live fills intended to redeem to $1. |
| `Fill` | autoinc | Per-leg fill within a basket. |
| `Resolution` | `event_id` | Winning outcome token id when oracle resolves. Drives basket redemption + paper PnL. |

## The hard problems (and how we mitigate)

### 1. Executable edge ≠ quoted edge
The opportunity engine **walks the book on every leg** and computes VWAP for the chosen basket size. The reported `net_edge` and `max_baskets` are clipped by the thinnest leg's depth. Top-of-book is never trusted on its own.

### 2. Multi-leg fill races (live mode)
Strategy: submit all N leg orders in parallel as **FAK (fill-and-kill)**. If any leg partial-fills, immediately try to unwind the over-filled legs at market. Persist every attempt for forensics. The paper executor models this by simulating the same FAK semantics against the observed book.

### 3. Fees, gas, and the "amortized gas trap"
Gas to submit N orders + 1 redeem dominates edge on small baskets. The opportunity engine computes `min_basket_count` such that amortized gas < edge, and refuses to fire below that.

### 4. Resolution risk (UMA disputes)
A "guaranteed" basket can pay $0 if the market resolves "invalid" or gets adversarially redirected. Mitigations:
- Skip events within 24h of `end_date` (price action there is dominated by resolution-game theory).
- Per-event exposure cap (`MAX_BASKET_USD` × `MAX_OPEN_BASKETS_PER_EVENT=1`).
- Persist a denylist of past disputed/invalid markets.

### 5. Paper-mode realism
Naive paper trading assumes you fill at the book at observation time. Real bots get beaten by faster ones. The simulator delays our hypothetical order arrival by `ARB_PAPER_LATENCY_MS` (default 250ms): we only fill against price levels that survived for that latency window, modeling the front-run penalty. This biases paper PnL **down**, which is the safe direction.

## Lifecycle: paper basket

1. Engine emits `Opportunity{event_id, legs[], net_edge_bps, max_baskets}`
2. Risk gate accepts (under all caps, edge ≥ threshold)
3. Paper executor creates `paper_baskets` row with status=`open`
4. For each leg: walk the book at `t = detected_at + paper_latency_ms`, compute fills, write `paper_fills` rows
5. If any leg lacks depth at the latency-shifted moment → status=`failed`, no PnL impact (this is the "missed it" case)
6. Otherwise status=`pending_resolution`, accumulate `total_cost_usd`
7. Resolution watcher polls Gamma `/events?id=...` for `closed=true`. On resolve: status=`redeemed`, `realized_pnl_usd = (1 * basket_count) - total_cost_usd` (or `-total_cost_usd` if invalid)

## Lifecycle: live basket

Identical to paper through step 2. Then:

3. Live executor creates `live_orders` rows for each leg with status=`pending`
4. Submit all N orders in parallel (FAK) via `py-clob-client`
5. Poll fills (or use `user` WS channel)
6. If complete-set held → call `NegRiskAdapter.redeemPositions(...)` with current gas estimate; record tx hash
7. If partial → submit market unwind orders for the over-filled legs; record net cost
8. Resolution watcher mirrors paper for accurate post-mortem PnL on un-redeemed legacy baskets

## Hard caps (both modes)

| Cap | Default | Source |
|---|---|---|
| Min net edge | 50 bps | `ARB_MIN_NET_EDGE_BPS` |
| Max basket USD | $50 | `ARB_MAX_BASKET_USD` |
| Max open baskets (global) | 3 | `ARB_MAX_OPEN_BASKETS` |
| Max open baskets per event | 1 | hard-coded |
| Daily loss stop | $100 | `ARB_DAILY_LOSS_STOP_USD` |
| Kill switch | `./KILL` file presence | `ARB_KILL_SWITCH_FILE` |
| Resolution proximity skip | 24h | hard-coded |

## Out of scope for v1

- Cross-platform (Kalshi, Manifold, sportsbooks) — separate funding paths, 2× KYC, no atomic exec.
- Vanilla CTF / non-negRisk arbs — the redeem path differs and the sum-to-1 invariant is more fragile.
- Maker (passive) strategies — we are takers only.
- Options-replication arbs vs Deribit — needs a vol-pricing layer.

## Phased build

| Phase | Scope | Capital risk |
|---|---|---|
| 1 | REST discovery + WS book + opportunity engine + paper executor + dashboard | $0 |
| 2 | Live executor (caps minimum), redeem path | ≤ $50/basket × 3 baskets |
| 3 | Resolution watcher, daily PnL accounting, denylist | same |
| 4 | Scale caps with proven track record; backtest harness | configurable |
