# Polymarket CLOB Order Signing Cookbook (`py-clob-client`)

A copy-pasteable reference for signing and submitting Polymarket CLOB orders from
Python. Verified against `py-clob-client` **v0.34.6** (released 2026-02-19).

All citations point at `Polymarket/py-clob-client@main` on GitHub. Update the pin
when bumping.

---

## 1. Install

Pin the exact version your executor was tested against. As of April 2026 the
latest published release is **0.34.6**.

```bash
pip install py-clob-client==0.34.6
```

Source: <https://pypi.org/project/py-clob-client/0.34.6/> /
[`setup.py` L7-L25](https://github.com/Polymarket/py-clob-client/blob/main/setup.py#L7-L25)

Transitive deps it pulls in (from `setup.py`):

- `eth-account>=0.13.0`
- `eth-utils>=4.1.1`
- `poly_eip712_structs>=0.0.1`
- `py-order-utils>=0.3.2`
- `py-builder-signing-sdk>=0.0.2`
- `httpx[http2]>=0.27.0`
- `python-dotenv`

Requires Python **3.9.10+**.

The HTTP client under the hood is `httpx` (sync). All `client.*` methods are
**blocking**. See the `asyncio` pattern in section 7.

---

## 2. One-Time Setup: Derive L2 API Credentials

L2 (HMAC) creds — `api_key`, `api_secret`, `api_passphrase` — are deterministic
for a given `(wallet, nonce)`. You generate them once and store them. The
`create_or_derive_api_creds()` helper tries `POST /auth/api-key` first, and falls
back to `GET /auth/derive-api-key` if the key already exists.

Reference:
[`py_clob_client/client.py` L211-L260](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/client.py#L211-L260)

```python
# scripts/bootstrap_clob_creds.py
"""
Run ONCE per wallet to mint L2 API credentials, then store the three
strings (api_key, api_secret, api_passphrase) in your secret manager.
"""
import os
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

HOST = "https://clob.polymarket.com"
PRIVATE_KEY = os.environ["POLY_PK"]          # 0x-prefixed hex
CHAIN_ID = POLYGON                            # 137 (mainnet) or AMOY=80002

# L1 client = host + chain + key. No creds needed yet.
client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)

# Idempotent: creates if missing, derives if existing. Returns ApiCreds.
creds = client.create_or_derive_api_creds()

print("CLOB_API_KEY     =", creds.api_key)
print("CLOB_SECRET      =", creds.api_secret)
print("CLOB_PASS_PHRASE =", creds.api_passphrase)
```

`ApiCreds` is a dataclass with three string fields:
[`clob_types.py` L19-L23](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/clob_types.py#L19-L23).

> Polymarket prints a giant warning that creds **cannot be recovered** if lost
> — store them in your secrets backend immediately.
> See [`constants.py` L7-L10](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/constants.py#L7-L10).

---

## 3. Client Init

```python
import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.constants import POLYGON

client = ClobClient(
    host="https://clob.polymarket.com",
    key=os.environ["POLY_PK"],                 # private key of the *signer* EOA
    chain_id=POLYGON,                          # 137
    creds=ApiCreds(
        api_key=os.environ["CLOB_API_KEY"],
        api_secret=os.environ["CLOB_SECRET"],
        api_passphrase=os.environ["CLOB_PASS_PHRASE"],
    ),
    signature_type=2,                          # see table below
    funder="0xYourPolymarketProxyAddress",     # USDC-holding address
)
```

Constructor signature:
[`client.py` L116-L165](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/client.py#L116-L165).

### `signature_type`

The integer is forwarded to `OrderBuilder.__init__` and stamped into the EIP-712
order payload as `signatureType`
([`order_builder/builder.py` L40-L49](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/order_builder/builder.py#L40-L49),
[L143](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/order_builder/builder.py#L143)).

| `signature_type` | Wallet Model                          | When to use                                                                                |
| ---------------- | ------------------------------------- | ------------------------------------------------------------------------------------------ |
| `0` (default, `EOA`) | Plain EOA / MetaMask / hardware   | Signer EOA *is* the funder. USDC and CTF tokens sit on the same address that signs.        |
| `1` (`POLY_PROXY`)   | Polymarket proxy (Magic / email)  | Signer EOA is the session key; funds live in a Polymarket-deployed proxy contract.         |
| `2` (`POLY_GNOSIS_SAFE`) | Gnosis Safe / browser proxy   | Signer EOA is an owner; funds live in a Safe / proxy contract.                             |

Default if omitted is `EOA` (0). See `EOA` constant in
`py_order_utils.model` re-exported via `builder.py` L4.

### `funder`

The address that **holds USDC and conditional tokens**. This goes into the
`maker` field of the signed order
([`builder.py` L132-L144](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/order_builder/builder.py#L132-L144)),
while `signer` is set to the address derived from your private key.

- If `funder` is omitted, it defaults to `signer.address()`.
- For arbitrage from a Polymarket UI account, `funder` = your visible Polymarket
  proxy address (look it up on polygonscan or in the UI), and `key` = the
  session/EOA key Polymarket gave you.
- For a pure EOA setup, leave `funder=None` (or pass the same address as the
  signer) and use `signature_type=0`.

### Read-only mode

Drop `creds`, `key`, `signature_type`, `funder` for L0 (public endpoints only):

```python
client = ClobClient("https://clob.polymarket.com")
client.get_order_book(token_id)
```

---

## 4. Place an Order

### 4a. Limit order — GTC (resting)

Source pattern:
[`examples/order.py` L1-L36](https://github.com/Polymarket/py-clob-client/blob/main/examples/order.py).

```python
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

order_args = OrderArgs(
    token_id="71321045679252212594626385532706912750332728571942532289631379312455583992563",
    price=0.42,        # USD per share, between 0.0 and 1.0
    size=100.0,        # shares
    side=BUY,          # or SELL
)

signed = client.create_order(order_args)        # builds EIP-712 + signs
resp   = client.post_order(signed, OrderType.GTC)
# resp == {"success": True, "orderID": "0x...", "status": "matched"|"live"|...}
```

`OrderArgs` fields (from
[`clob_types.py` L42-L83](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/clob_types.py#L42-L83)):
`token_id`, `price`, `size`, `side`, `fee_rate_bps=0`, `nonce=0`,
`expiration=0`, `taker=ZERO_ADDRESS`.

`create_order` automatically:

- fetches and caches the market's `tick_size` and `neg_risk` flag
  ([`client.py` L402-L448, L492-L535](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/client.py#L492-L535)),
- validates your price against the tick,
- picks the correct exchange contract for `neg_risk` markets,
- signs an EIP-712 order with `maker=funder`, `signer=EOA`, `signatureType=...`.

### 4b. Limit order — GTD (good-till-date)

Source: [`examples/GTD_order.py`](https://github.com/Polymarket/py-clob-client/blob/main/examples/GTD_order.py).

```python
order_args = OrderArgs(
    token_id="...",
    price=0.50,
    size=100.0,
    side=BUY,
    expiration="1000000000000",   # unix seconds; must be > now+60s
)
signed = client.create_order(order_args)
resp   = client.post_order(signed, OrderType.GTD)
```

### 4c. FOK (Fill-Or-Kill)

FOK requires the entire size to fill immediately at the limit price or better,
otherwise the whole order is cancelled. Polymarket uses FOK for *market* buys
priced in dollars (`MarketOrderArgs.amount`).

Source: [`examples/market_buy_order.py`](https://github.com/Polymarket/py-clob-client/blob/main/examples/market_buy_order.py).

```python
from py_clob_client.clob_types import MarketOrderArgs, OrderType

mo = MarketOrderArgs(
    token_id="...",
    amount=100.0,   # BUY: USDC to spend.  SELL: shares to sell.
    side=BUY,
)
signed = client.create_market_order(mo)
resp   = client.post_order(signed, orderType=OrderType.FOK)
```

`MarketOrderArgs` defaults `order_type=OrderType.FOK`
([`clob_types.py` L86-L122](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/clob_types.py#L86-L122)).

### 4d. FAK / IOC (Fill-And-Kill, a.k.a. Immediate-Or-Cancel)

`OrderType.FAK` is Polymarket's IOC variant — fills as much as possible
immediately, cancels the unfilled remainder. Use this for arbitrage legs where
partial fills are acceptable.

```python
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

order_args = OrderArgs(token_id="...", price=0.42, size=100.0, side=BUY)
signed = client.create_order(order_args)
resp   = client.post_order(signed, OrderType.FAK)   # IOC behavior
```

Enum values:
[`clob_types.py` L11-L16`](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/clob_types.py#L11-L16):

```python
class OrderType(enumerate):
    GTC = "GTC"   # resting limit
    FOK = "FOK"   # all-or-nothing immediate
    GTD = "GTD"   # resting limit with expiry
    FAK = "FAK"   # IOC: fill what you can, cancel rest
```

> **TL;DR for an arbitrage executor**: use `FAK` for legs where you want IOC
> semantics on a limit order, and `FOK` for $-denominated market-sweep buys
> where you only want the trade if the full notional clears.

`post_only=True` is only legal with `GTC` / `GTD`
([`client.py` L623-L628](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/client.py#L623-L628)).

---

## 5. Cancel Orders

Source: [`examples/cancel_order.py`](https://github.com/Polymarket/py-clob-client/blob/main/examples/cancel_order.py),
[`examples/cancel_orders.py`](https://github.com/Polymarket/py-clob-client/blob/main/examples/cancel_orders.py).

```python
# single
client.cancel(order_id="0xabc...")

# batch
client.cancel_orders(["0xabc...", "0xdef..."])

# all open orders for this API key
client.cancel_all()

# all orders on a market or token
client.cancel_market_orders(market="0x...condition_id...", asset_id="")
client.cancel_market_orders(market="", asset_id="<token_id>")
```

Implementations:
[`client.py` L663-L748](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/client.py#L663-L748).

---

## 6. Get Positions / Open Orders / Fills

py-clob-client does **not** ship a `get_positions()` method — Polymarket exposes
"positions" via the Data-API (separate service). Within `py-clob-client` you use
**balance/allowance** for current token holdings, **`get_orders`** for open
orders, and **`get_trades`** for fills.

### Balance / position per token

Source: [`examples/get_balance_allowance.py`](https://github.com/Polymarket/py-clob-client/blob/main/examples/get_balance_allowance.py).

```python
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

# USDC balance + exchange allowance
usdc = client.get_balance_allowance(
    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
)

# Conditional-token (outcome share) balance for one token_id
shares = client.get_balance_allowance(
    BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL,
        token_id="71321045679252212594626385532706912750332728571942532289631379312455583992563",
    )
)
```

Note: `BalanceAllowanceParams.signature_type` defaults to `-1` and is auto-filled
from the client.

### Open orders

```python
from py_clob_client.clob_types import OpenOrderParams

orders = client.get_orders(OpenOrderParams())                       # all
orders = client.get_orders(OpenOrderParams(market="0x...condition")) # one market
orders = client.get_orders(OpenOrderParams(asset_id="<token_id>"))   # one token
```

`get_orders` paginates internally with `next_cursor`
([`client.py` L750-L769](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/client.py#L750-L769)).

### Fills (trades)

Source: [`examples/get_trades.py`](https://github.com/Polymarket/py-clob-client/blob/main/examples/get_trades.py).

```python
from py_clob_client.clob_types import TradeParams

trades = client.get_trades(
    TradeParams(
        maker_address=client.get_address(),
        market="0x5f65177b394277fd294cd75650044e32ba009a95022d88a0c1d565897d72f8f1",
    )
)
```

---

## 7. Multi-Leg / Parallel Order Submission

`py-clob-client` is **synchronous** (it uses `httpx` in blocking mode). For
arbitrage you have two good options:

### Option A — Server-side batch (preferred when atomicity matters less)

`post_orders` ships N orders in one HTTP round-trip. Lower latency than N
parallel calls, but the server processes them serially.

Source: [`examples/orders.py`](https://github.com/Polymarket/py-clob-client/blob/main/examples/orders.py).

```python
from py_clob_client.clob_types import OrderArgs, PostOrdersArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

resp = client.post_orders([
    PostOrdersArgs(
        order=client.create_order(OrderArgs(
            token_id="...YES_TOKEN_ID...",
            price=0.50, size=100, side=BUY)),
        orderType=OrderType.FAK,
        postOnly=False,
    ),
    PostOrdersArgs(
        order=client.create_order(OrderArgs(
            token_id="...NO_TOKEN_ID...",
            price=0.51, size=100, side=BUY)),
        orderType=OrderType.FAK,
        postOnly=False,
    ),
])
```

Implementation: [`client.py` L592-L621](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/client.py#L592-L621).

### Option B — `asyncio.gather` over a thread pool (true parallel HTTP)

When you want each leg to be a separate request fired concurrently (useful for
hitting the matching engine at the same time across markets), wrap the sync
client with `loop.run_in_executor`:

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

# One executor for the whole process is fine. Size = max parallel legs.
_EXECUTOR = ThreadPoolExecutor(max_workers=16)

async def submit_leg(client, order_args: OrderArgs, order_type: OrderType):
    loop = asyncio.get_running_loop()
    # create_order signs (CPU + 1 cached HTTP call for tick/neg_risk),
    # post_order does the actual order POST.
    signed = await loop.run_in_executor(_EXECUTOR, client.create_order, order_args)
    return await loop.run_in_executor(
        _EXECUTOR, client.post_order, signed, order_type
    )

async def execute_arb(client, legs: list[tuple[OrderArgs, OrderType]]):
    return await asyncio.gather(
        *(submit_leg(client, args, ot) for args, ot in legs),
        return_exceptions=True,   # don't let one failure cancel the others
    )

# Usage
legs = [
    (OrderArgs(token_id=YES, price=0.50, size=100, side=BUY), OrderType.FAK),
    (OrderArgs(token_id=NO,  price=0.51, size=100, side=BUY), OrderType.FAK),
]
results = asyncio.run(execute_arb(client, legs))
```

Notes:

- The `ClobClient` instance is safe to share across threads for read paths and
  for `post_order`. Each call constructs its own `httpx` request.
- Pre-warm tick/`neg_risk` caches by calling `client.get_tick_size(token_id)`
  and `client.get_neg_risk(token_id)` once at startup so the hot path skips two
  HTTP round trips per `create_order`. Caches live on the client
  ([`client.py` L156-L160, L402-L448](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/client.py#L402-L448)).
- The tick-size cache TTL is configurable via the `tick_size_ttl` ctor arg
  (default 300s).

---

## 8. NegRisk Markets

NegRisk ("negative-risk") markets are Polymarket's multi-outcome markets where
the YES tokens of all outcomes sum to ~$1. They use a **different exchange
contract** than vanilla binary markets, but the SDK handles the routing for you.

### What you do NOT need to do

`OrderArgs` is **identical** for negRisk and vanilla tokens — you still pass
`token_id`, `price`, `size`, `side`. There is no `neg_risk` field on
`OrderArgs`.

### What the SDK does behind the scenes

When you call `client.create_order(...)`, it:

1. Calls `GET /neg-risk?token_id=...` to learn whether the token belongs to a
   negRisk market (cached forever per `token_id` —
   [`client.py` L441-L448](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/client.py#L441-L448)).
2. Calls `get_contract_config(chain_id, neg_risk=True/False)` to pick the right
   exchange address ([`builder.py` L146-L154](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/order_builder/builder.py#L146-L154)).
3. Signs the EIP-712 payload against that exchange's domain separator.

### When you DO need to override

If you already know the market is negRisk and want to skip the lookup, pass
`PartialCreateOrderOptions`:

```python
from py_clob_client.clob_types import PartialCreateOrderOptions

signed = client.create_order(
    order_args,
    options=PartialCreateOrderOptions(neg_risk=True, tick_size="0.01"),
)
```

(definition:
[`clob_types.py` L165-L172](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/clob_types.py#L165-L172).)

### Allowances (one-time, per signer)

For EOAs (`signature_type=0`) you must approve **both** the vanilla CTF Exchange
**and** the NegRisk CTF Exchange + NegRisk Adapter on Polygon mainnet.
Magic/proxy wallets (`signature_type=1` or `2`) have allowances set
automatically. From the README:

| Token                                   | Approve for                                   |
| --------------------------------------- | --------------------------------------------- |
| USDC (`0x2791Bca1...A84174`)            | `0x4bFb41d5...8B8982E` (CTF Exchange)         |
| Conditional Tokens (`0x4D97DCd9...476045`) | `0xC5d563A3...220f80a` (NegRisk Exchange) |
|                                         | `0xd91E80cF...0DA35296` (NegRisk Adapter)     |

Reference allowance script (linked in the README):
<https://gist.github.com/poly-rodr/44313920481de58d5a3f6d1f8226bd5e>

---

## 9. Error Handling

### Exception hierarchy

All client exceptions live in
[`py_clob_client/exceptions.py`](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/exceptions.py):

```python
class PolyException(Exception):
    msg: str

class PolyApiException(PolyException):
    status_code: int | None    # HTTP status from the failed response
    error_msg: dict | str      # parsed JSON or raw text body
```

`PolyApiException` is raised inside `http_helpers/helpers.py` for any non-2xx
HTTP response. `httpx` exceptions (`httpx.RequestError`,
`httpx.TimeoutException`, `httpx.HTTPError`) can leak through on network/DNS
failures.

### Recommended catch ladder

```python
import httpx
from py_clob_client.exceptions import PolyApiException, PolyException

try:
    resp = client.post_order(signed, OrderType.FAK)
except PolyApiException as e:
    # API-layer failure: invalid price, insufficient balance, market closed, 4xx/5xx
    if e.status_code in (429, 502, 503, 504):
        # rate-limited or transient — retry with backoff
        ...
    elif e.status_code in (400, 422):
        # client error — DO NOT retry; surface to operator
        ...
    else:
        ...
except (httpx.TimeoutException, httpx.RequestError) as e:
    # Network-layer failure — safe to retry idempotently if you used a fresh nonce
    ...
except PolyException as e:
    # Local SDK validation (e.g., invalid tick size, bad side)
    ...
```

### Retry guidance for an arbitrage executor

- **Idempotency**: Polymarket assigns the order ID on the server side from the
  EIP-712 hash, so re-posting the *exact same signed payload* is naturally
  idempotent for `GTC`/`GTD`. For `FAK`/`FOK`, the hash includes the salt so a
  fresh `create_order` call generates a *new* order — only retry if you
  confirmed via `get_orders` / `get_trades` that the original did not fill.
- **Cap retries at 1-2** for any order-placement call; latency-sensitive
  arbitrage prefers fast failure over duplicated risk.
- **Pre-flight checks** before any loop: `client.get_balance_allowance(...)`
  for both legs, `client.get_tick_size(token_id)` to warm the cache.
- **Heartbeat-based dead-man switch**: `client.post_heartbeat(heartbeat_id)`
  cancels all your orders if no heartbeat arrives within 10s — useful as a
  safety net while the executor is running
  ([`client.py` L713-L727](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/client.py#L713-L727)).

### Common API-side rejection reasons

- `not enough balance / allowance` — top up USDC or re-run the allowance script.
- `min size not met` — `OrderBookSummary.min_order_size`.
- `tick size invalid` — your `price` doesn't fit the market's tick. Use
  `client.get_tick_size(token_id)` and round.
- `market not active` — market is paused, resolved, or closed.
- `order expired` — for `GTD`, `expiration` must be > `now + 60s`.

---

## 10. Constants Cheat Sheet

```python
from py_clob_client.constants import POLYGON, AMOY    # 137, 80002
from py_clob_client.order_builder.constants import BUY, SELL   # "BUY", "SELL"
from py_clob_client.clob_types import OrderType, AssetType
# OrderType.GTC | FOK | GTD | FAK
# AssetType.COLLATERAL | CONDITIONAL
```

`POLYGON = 137`, `AMOY = 80002` (testnet) — see
[`constants.py`](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/constants.py).

Default CLOB host: `https://clob.polymarket.com`.
