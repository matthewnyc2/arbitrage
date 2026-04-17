# Polymarket Arbitrage Scanner

A production-grade Python asyncio scanner that detects **NegRisk multi-outcome
arbitrage** on Polymarket, simulates fills in paper mode with realistic
latency penalties, and can sign + submit live orders through `py-clob-client`
behind hard risk gates.

**[Live browser demo](https://matthewnyc2.github.io/arbitrage/demo.html)** ·
**[Project overview](https://matthewnyc2.github.io/arbitrage/)** ·
**[Architecture](DESIGN.md)**

![tests](https://img.shields.io/badge/tests-60%20passing-brightgreen)
![coverage](https://img.shields.io/badge/core%20math-94%25-brightgreen)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-MIT-blue)

---

## The arbitrage

Polymarket hosts *categorical* events (e.g. "Who wins the 2028 Election?")
where every outcome trades as its own YES token. Because exactly one outcome
must win, the fair prices across all outcomes must sum to $1. When the
*sum of best-asks* across every outcome drops below $1, buying a complete
set is a guaranteed $1 payout — a risk-free arbitrage.

```
Σ best_ask(outcome_i)  <  $1.00   ⟹   buy one of each, redeem for $1
```

The scanner watches the live CLOB, walks the order book depth to size each
leg honestly, subtracts fees and amortised Polygon gas, and emits sized
opportunities in real time.

## Quickstart

```bash
git clone https://github.com/matthewnyc2/arbitrage
cd arbitrage
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env

arb init        # create SQLite schema
arb discover    # pull active negRisk events from Polymarket
arb scan &      # start the paper scanner (WS + engine + executor)
arb web         # dashboard at http://127.0.0.1:8000
```

### Or with Docker

```bash
docker compose up
# dashboard at http://127.0.0.1:8000
```

## What you're looking at

| Piece | File | Purpose |
|---|---|---|
| Gamma REST discovery | `arbitrage/clients/polymarket_rest.py` | Paginates `/events`, filters to active negRisk categoricals, upserts to SQLite |
| WebSocket L2 book maintainer | `arbitrage/clients/polymarket_ws.py` + `arbitrage/book/l2.py` | Subscribes to CLOB market channel, parses `book` / `price_change` events, maintains per-token sorted ladders with desync detection |
| Opportunity engine | `arbitrage/engine/opportunity.py` | On every book tick, walks depth on every outcome, computes VWAP basket cost, picks the size that maximizes net expected profit after fees + gas |
| Paper executor | `arbitrage/engine/paper_fills.py` | Simulates IOC fills at `detection + latency_ms` against the live book, writes baskets + fills to SQLite, marks PnL on resolution |
| Live executor | `arbitrage/engine/live_executor.py` | Signs EIP-712 orders via `py-clob-client`, submits FAK in parallel across legs, unwinds partial fills, redeems on resolution. Gated behind `ARB_MODE=live` + risk caps |
| Risk gate | `arbitrage/engine/live_executor.py::risk_gate` | Hard caps: basket USD, open-basket count (global + per-event), daily loss stop, kill-switch file |
| Dashboard | `arbitrage/web/app.py` + `templates/` | FastAPI + HTMX single page, auto-refreshing tables, one-click kill switch |
| CLI | `arbitrage/cli.py` | `arb init \| discover \| scan \| web \| resolve` |

## Architecture

```
        Gamma REST                 CLOB WebSocket
            │                            │
            ▼                            ▼
     Event discovery              L2 Book Maintainer
    (active negRisk)           (per token, in-memory)
            │                            │
            └────────────┬───────────────┘
                         ▼
                Opportunity Engine
      (depth-walk, fee-net, gas-amortized threshold)
                         │
                         ▼
                    Risk Gate
       (basket caps, daily loss, kill switch)
                         │
            ┌────────────┴───────────────┐
            ▼                            ▼
      Paper Executor              Live Executor
   (latency-penalized          (sign + FAK + redeem)
    sim fills + PnL)                     │
            │                            │
            └────────────┬───────────────┘
                         ▼
                    SQLite (WAL)
                         │
                         ▼
             FastAPI + HTMX dashboard
```

## Modes

- **`ARB_MODE=paper`** (default) — real data, simulated fills, no keys touched.
  Paper baskets sit `pending_resolution` until the underlying market closes,
  then flip to `redeemed` or `invalid` and realized PnL is booked.
- **`ARB_MODE=live`** — signs and submits real orders, redeems complete sets
  via `NegRiskAdapter.redeemPositions`. `LiveExecutor.dry_run=True` by default
  so orders are logged rather than broadcast until an operator explicitly
  flips the flag.

## Safety rails

| Cap | Env var | Default |
|---|---|---|
| Minimum net edge (bps) | `ARB_MIN_NET_EDGE_BPS` | 50 |
| Max USD per basket | `ARB_MAX_BASKET_USD` | 50 |
| Max open baskets (global) | `ARB_MAX_OPEN_BASKETS` | 3 |
| Max open baskets per event | hardcoded | 1 |
| Daily loss stop (USD) | `ARB_DAILY_LOSS_STOP_USD` | 100 |
| Kill switch file | `ARB_KILL_SWITCH_FILE` | `./KILL` |
| Paper-mode latency penalty (ms) | `ARB_PAPER_LATENCY_MS` | 250 |

The dashboard has a red **kill** button that touches the kill-switch file;
the executor refuses to open any new baskets while that file exists.

## Test suite

```bash
pytest                              # 60 tests, ~5s
pytest --cov=arbitrage              # with coverage
```

Coverage on the core math layers:

| Module | Coverage |
|---|---:|
| `arbitrage/book/l2.py` | 94% |
| `arbitrage/engine/paper_fills.py` | 94% |
| `arbitrage/web/app.py` | 92% |
| `arbitrage/db.py` | 100% |
| `arbitrage/engine/opportunity.py` | 81% |

## Tech stack

Python 3.12 · asyncio · pydantic v2 · FastAPI + HTMX + Jinja2 · SQLite (WAL) ·
`py-clob-client` · `web3.py` · `httpx` · `websockets` · `tenacity` · `loguru` ·
pytest + pytest-asyncio · Docker

## Status

Phase 1 (paper-workable) is complete — scanner runs end-to-end, all 60 tests
pass, CI green. Live executor skeleton is in place but gated. See
[`STATUS.html`](STATUS.html) for an honest plain-English breakdown of what
works, what doesn't, and what it would take to run this in anger.

## License

MIT. See `LICENSE`.
