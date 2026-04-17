# Polymarket NegRisk Reference (Polygon mainnet)

Single-page reference for arbitraging categorical Polymarket events where
`Σ(best-ask of every outcome) < $1`. Covers the contracts, ABI, lifecycle, Gamma
API detection, ID derivation, a runnable web3.py snippet, and gotchas.

All sources cited inline. Last verified: April 2026.

---

## 1. Concept

Vanilla Polymarket markets use Gnosis's **Conditional Token Framework (CTF)**:
each binary market mints a YES and a NO ERC-1155 token, fully collateralized 1:1
by USDC.e. Splitting 1 USDC.e gives `1 YES + 1 NO`; merging the pair returns
1 USDC.e; after resolution the winning side redeems for 1 USDC.e each.

A **categorical event** ("Who wins the 2028 US Election?") is modeled as N
independent binary markets — one per candidate. Without negRisk these markets
are unconnected, which means a holder of `NO` on every candidate is locked up
even though, by construction, exactly one of them must resolve YES. NegRisk
fixes this: the **NegRiskAdapter** wraps the underlying CTF and adds a
`convertPositions` operation: 1 NO share in market *i* of an event can be
atomically converted into 1 YES share in **every other** market of that event.
That makes a complete set of YES tokens (one per outcome) economically
equivalent to $1 USDC.e and lets capital be freed early instead of waiting for
oracle resolution. This is the property the arb strategy exploits — when the
best-ask sum of every outcome's YES token is below $1, you can buy a complete
set, redeem (or convert+redeem), and lock in the spread.
([NegRisk overview](https://docs.polymarket.com/developers/neg-risk/overview),
[neg-risk-ctf-adapter README](https://github.com/Polymarket/neg-risk-ctf-adapter),
[ChainSecurity audit, Apr 2024](https://old.chainsecurity.com/wp-content/uploads/2024/04/ChainSecurity_Polymarket_NegRiskAdapter_audit.pdf))

---

## 2. Contract addresses (Polygon mainnet, chainId 137)

Source: [Polymarket Contract Addresses](https://docs.polymarket.com/resources/contract-addresses),
cross-checked on PolygonScan.

| Contract | Address | PolygonScan |
|---|---|---|
| **NegRiskAdapter** | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` | [link](https://polygonscan.com/address/0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296) |
| **NegRiskCtfExchange** | `0xC5d563A36AE78145C45a50134d48A1215220f80a` | [link](https://polygonscan.com/address/0xc5d563a36ae78145c45a50134d48a1215220f80a) |
| **NegRiskFeeModule** | `0x78769D50Be1763ed1CA0D5E878D93f05aabff29e` | [link](https://polygonscan.com/address/0x78769d50be1763ed1ca0d5e878d93f05aabff29e) |
| **CTFExchange** (vanilla) | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` | [link](https://polygonscan.com/address/0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e) |
| **ConditionalTokens (CTF)** | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | [link](https://polygonscan.com/address/0x4d97dcd97ec945f40cf65f87097ace5ea0476045) |
| **USDC.e (collateral)** | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` | [link](https://polygonscan.com/address/0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174) |
| **UmaCtfAdapter** (oracle) | `0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74` | [link](https://polygonscan.com/address/0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74) |
| **UMA Optimistic Oracle** | `0xCB1822859cEF82Cd2Eb4E6276C7916e692995130` | [link](https://polygonscan.com/address/0xCB1822859cEF82Cd2Eb4E6276C7916e692995130) |

> The collateral is **USDC.e** (the bridged PoS USDC), **not** native
> Circle-issued USDC (`0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359`). Confusing
> these two will silently break allowance checks. See Gotchas §8.

---

## 3. Key ABI signatures

The NegRiskAdapter exposes both vanilla CTF-shaped overloads (so it can act as
a drop-in `IConditionalTokens` proxy) and negRisk-specific entrypoints. Source:
[`NegRiskAdapter.sol`](https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/src/NegRiskAdapter.sol),
[`docs/NegRiskAdapter.md`](https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/docs/NegRiskAdapter.md).

### 3.1 Position management (call these on the NegRiskAdapter)

```solidity
// Mint a complete set: deposits `_amount` USDC.e, mints `_amount` of each YES & NO.
function splitPosition(bytes32 _conditionId, uint256 _amount) external;

// Burn a complete set (YES + NO of every outcome of the condition) for USDC.e.
function mergePositions(bytes32 _conditionId, uint256 _amount) external;

// After UMA resolution, redeem held outcome tokens for USDC.e payout.
// _amounts[i] is the amount of outcome-i token to burn.
function redeemPositions(bytes32 _conditionId, uint256[] calldata _amounts) external;

// negRisk-specific: convert NO shares in markets selected by `_indexSet`
// (a bitmask over the marketId's questions) into YES shares of the rest + collateral.
function convertPositions(bytes32 _marketId, uint256 _indexSet, uint256 _amount) external;
```

There are also legacy 5-arg overloads kept for `IConditionalTokens` API parity
(unused by clients in practice):

```solidity
function splitPosition(address _collateralToken, bytes32, bytes32 _conditionId,
                       uint256[] calldata, uint256 _amount) external;
function mergePositions(address _collateralToken, bytes32, bytes32 _conditionId,
                        uint256[] calldata, uint256 _amount) external;
```

### 3.2 ID lookups (view)

```solidity
function getConditionId(bytes32 _questionId) external view returns (bytes32);
function getPositionId(bytes32 _questionId, bool _outcome) external view returns (uint256);
function balanceOf(address _owner, uint256 _id) external view returns (uint256);
function balanceOfBatch(address[] memory _owners, uint256[] memory _ids)
        external view returns (uint256[] memory);
```

### 3.3 Admin / oracle (you will not call these, but useful for tracing)

```solidity
function prepareMarket(uint256 _feeBips, bytes calldata _metadata) external returns (bytes32);
function prepareQuestion(bytes32 _marketId, bytes calldata _metadata) external returns (bytes32);
function reportOutcome(bytes32 _questionId, bool _outcome) external; // onlyOperator
```

### 3.4 Required ERC-20 / ERC-1155 approvals

Before any of the above, set:

```solidity
// USDC.e:
IERC20(USDC_E).approve(NegRiskAdapter, type(uint256).max);

// ERC-1155 outcome tokens (for merge / redeem / convert):
IConditionalTokens(CTF).setApprovalForAll(NegRiskAdapter, true);
```

(Source: [`NegRiskAdapter.sol`](https://raw.githubusercontent.com/Polymarket/neg-risk-ctf-adapter/main/src/NegRiskAdapter.sol))

---

## 4. End-to-end arb lifecycle

For a categorical event with N outcomes where `Σ best_ask_i < 1`:

1. **Approvals (one-time per wallet)**
   - `USDC.e.approve(NegRiskAdapter, 2^256-1)`
   - `ConditionalTokens.setApprovalForAll(NegRiskAdapter, true)`
   - Approvals required for the **NegRiskCtfExchange** (`0xC5d5...80a`) for
     trading: `USDC.e.approve(exchange, ...)` and
     `ConditionalTokens.setApprovalForAll(exchange, true)`.

2. **Buy a complete set via the Exchange**
   - Use the CLOB (`py-clob-client`, set `neg_risk=True` on the order options)
     to lift the best ask of each of the N outcome tokens for `size` shares.
     Total USDC.e spent ≈ `size * Σ best_ask_i`, which is < `size * $1`.
   - Equivalent: hit each `clobTokenIds[YES]` from the Gamma `markets[]` array.

3. **Redeem on resolution OR free capital early**
   - **Patient path**: wait for UMA to resolve the event and call
     `NegRiskAdapter.redeemPositions(conditionId_winner, [size, 0])` on the
     winning binary market. Payout = `size * 1 USDC.e`. Profit
     = `size * (1 − Σ best_ask_i)` minus gas and fees.
   - **Capital-recycling path** (the negRisk superpower): once you hold one
     YES of every outcome of the event, that bundle is economically `$1` per
     unit. Rather than redeem on each binary, you can `convertPositions` to
     consolidate, or simply burn the bundle: per the adapter, holding the
     full YES set is interchangeable with USDC.e, so a `mergePositions` on
     each conditionId (each binary has its own NO if you also hold it, or
     use `convert`) returns USDC.e instantly without waiting for the oracle.
     Practically: most arb bots redeem after resolution because acquiring a
     full NO+YES pair on every binary defeats the point — you bought only the
     YES legs for the discount.

4. **USDC.e arrives in your wallet.** Fees: NegRisk markets pay a small
   protocol fee on conversion (defined by `_feeBips` at `prepareMarket`
   time, paid to the Vault); redeem itself has no Polymarket fee.

---

## 5. Detecting negRisk markets via the Gamma API

Endpoint: `https://gamma-api.polymarket.com/events?...`

The two flags that matter on each `event` JSON object:

| JSON field | Type | Meaning |
|---|---|---|
| `negRisk` | bool | `true` → categorical event; outcomes are tied via the NegRiskAdapter. |
| `negRiskMarketID` | hex string (`bytes32`) | The shared `marketId` that links every binary in this event. Same value also appears on each child `markets[i].negRiskMarketID`. |
| `enableNegRisk` | bool | Set on a market when it can be added later as a new outcome (placeholder-capable). |
| `negRiskAugmented` | bool | Indicates the event has been augmented with such placeholder markets. |

The relevant fields inside each `markets[i]` element:

| JSON field | Use |
|---|---|
| `conditionId` (`bytes32`) | Pass to `redeemPositions` / `splitPosition`. |
| `questionID` (`bytes32`) | Source of `conditionId` via `getConditionId(questionID)`. |
| `clobTokenIds` | `[YES_tokenId, NO_tokenId]` as decimal strings. These are the ERC-1155 ids you reference to the CLOB order book. |
| `outcomePrices` | `["yes", "no"]` last-trade probabilities. Use `book` REST/WS for live best ask. |

Sample (trimmed) — `2026 FIFA World Cup Winner` event:

```json
{
  "id": "12345",
  "negRisk": true,
  "negRiskMarketID": "0xb5c32a9acd39848acad4913ac4cd49c5de2afcc9d23a8a7ba2419375fab87400",
  "markets": [
    {
      "questionID": "0x...",
      "conditionId": "0x7976b8dbacf9077eb1453a62bcefd6ab2df199acd28aad276ff0d920d6992892",
      "clobTokenIds": ["4394372887385518214471608448209527405727552777602031099972143344338178308080",
                       "112680630004798425069810935278212000865453267506345451433803052322987302357330"],
      "outcomePrices": ["0.1715","0.8285"],
      "negRiskMarketID": "0xb5c32a9acd39848acad4913ac4cd49c5de2afcc9d23a8a7ba2419375fab87400"
    },
    ...
  ]
}
```

Sample query to surface candidates:

```python
import requests
events = requests.get(
    "https://gamma-api.polymarket.com/events",
    params={"closed": "false", "limit": 200, "order": "volume24hr",
            "ascending": "false"},
    timeout=15,
).json()
neg_risk_events = [e for e in events if e.get("negRisk")]
for e in neg_risk_events:
    yes_asks = [float(m["outcomePrices"][0]) for m in e["markets"]]
    if sum(yes_asks) < 0.99:  # candidate; verify against live book
        print(e["title"], sum(yes_asks))
```

(Source: live Gamma API response; field list cross-checked against
[`Polymarket/agents/agents/polymarket/gamma.py`](https://github.com/Polymarket/agents/blob/main/agents/polymarket/gamma.py)
and [docs.polymarket.com/developers/neg-risk/overview](https://docs.polymarket.com/developers/neg-risk/overview).)

---

## 6. How `tokenId` and `conditionId` are derived

NegRisk markets reuse the underlying CTF derivation rules but plug a
**WrappedCollateral** ERC-20 in place of raw USDC.e. That changes which
collateral address goes into `positionId` — the one number that frequently
trips up new integrators.

### 6.1 Vanilla CTF (used by non-negRisk binary markets)

```text
conditionId  = keccak256( oracle ‖ questionId ‖ outcomeSlotCount )
collectionId = EC point-add of (parentCollectionId, hashToCurve(conditionId ‖ indexSet))
positionId   = uint256( keccak256( collateralToken ‖ collectionId ) )
```

For a vanilla binary market: `oracle = UmaCtfAdapter`, `outcomeSlotCount = 2`,
`collateralToken = USDC.e`, `indexSet = 1` for YES and `2` for NO.
(Source: [CTHelpers.sol](https://raw.githubusercontent.com/Polymarket/neg-risk-ctf-adapter/main/src/libraries/CTHelpers.sol))

### 6.2 NegRisk markets (the difference)

For each outcome of a categorical event, NegRisk creates an independent
binary CTF condition, **but** with two changes:

1. The CTF `oracle` field is set to the **NegRiskAdapter address**
   (`0xd91E…5296`) — not UmaCtfAdapter. The NegRiskAdapter is itself the
   thing that calls `reportPayouts` upstream.
2. The `collateralToken` baked into `positionId` is the
   **WrappedCollateral** ERC-20 (deployed by the adapter), not USDC.e. The
   adapter holds USDC.e and mints/burns wrapper tokens 1:1 against it.

Practically:

- `marketId` (the negRisk-level grouping) =
  `keccak256(operator ‖ feeBips ‖ metadata ‖ nonce)` — assigned at
  `prepareMarket` time and is what `negRiskMarketID` in Gamma exposes.
- `questionId` for the i-th binary in the event =
  `keccak256(marketId ‖ i)` (the index byte is the `_questionIndex`),
  which keeps all questions of a categorical event derivable from the
  single marketId.
- `conditionId = NegRiskAdapter.getConditionId(questionId)`
  = `keccak256(NegRiskAdapter ‖ questionId ‖ 2)`.
- `positionId(YES) = NegRiskAdapter.getPositionId(questionId, true)`
  `positionId(NO)  = NegRiskAdapter.getPositionId(questionId, false)` —
  these match the decimal `clobTokenIds` returned by the Gamma API.

> **In practice you do not recompute these.** Pull `conditionId` and
> `clobTokenIds` straight from Gamma; only call `getPositionId` /
> `getConditionId` if you want to verify against on-chain truth.

(Source: [`NegRiskAdapter.sol`](https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/src/NegRiskAdapter.sol),
[`MarketStateLib`](https://github.com/Polymarket/neg-risk-ctf-adapter/tree/main/src/libraries),
ChainSecurity audit §2.1)

---

## 7. End-to-end Python (web3.py) — approve + simulate redeem

Self-contained, structurally complete. Uses placeholder `0x...` for the
private key only. The redeem call is built but NOT broadcast — `call()` runs
it as an `eth_call` simulation.

```python
"""
Polymarket NegRisk arb — approval + redeem simulation on Polygon.
Requires: web3>=6.20, requests
"""
import os
import requests
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware  # PoA chain (Polygon)

# ---- 1. Connect ----------------------------------------------------------
RPC_URL = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
assert w3.is_connected(), "RPC down"

# ---- 2. Addresses (Polygon mainnet) --------------------------------------
NEG_RISK_ADAPTER  = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
NEG_RISK_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
CTF               = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
USDC_E            = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# ---- 3. Wallet (placeholder) --------------------------------------------
PRIVATE_KEY = os.getenv("PK", "0x" + "11" * 32)            # placeholder
acct        = w3.eth.account.from_key(PRIVATE_KEY)
ME          = acct.address

# ---- 4. Minimal ABIs -----------------------------------------------------
ERC20_ABI = [
    {"name":"approve","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
     "outputs":[{"type":"bool"}]},
    {"name":"allowance","type":"function","stateMutability":"view",
     "inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],
     "outputs":[{"type":"uint256"}]},
]

CTF_ABI = [
    {"name":"setApprovalForAll","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],
     "outputs":[]},
]

NEG_RISK_ADAPTER_ABI = [
    {"name":"splitPosition","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"_conditionId","type":"bytes32"},
               {"name":"_amount","type":"uint256"}], "outputs":[]},
    {"name":"mergePositions","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"_conditionId","type":"bytes32"},
               {"name":"_amount","type":"uint256"}], "outputs":[]},
    {"name":"redeemPositions","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"_conditionId","type":"bytes32"},
               {"name":"_amounts","type":"uint256[]"}], "outputs":[]},
    {"name":"convertPositions","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"_marketId","type":"bytes32"},
               {"name":"_indexSet","type":"uint256"},
               {"name":"_amount","type":"uint256"}], "outputs":[]},
    {"name":"getConditionId","type":"function","stateMutability":"view",
     "inputs":[{"name":"_questionId","type":"bytes32"}],
     "outputs":[{"type":"bytes32"}]},
    {"name":"getPositionId","type":"function","stateMutability":"view",
     "inputs":[{"name":"_questionId","type":"bytes32"},
               {"name":"_outcome","type":"bool"}],
     "outputs":[{"type":"uint256"}]},
]

usdc    = w3.eth.contract(address=USDC_E,            abi=ERC20_ABI)
ctf     = w3.eth.contract(address=CTF,               abi=CTF_ABI)
adapter = w3.eth.contract(address=NEG_RISK_ADAPTER,  abi=NEG_RISK_ADAPTER_ABI)

# ---- 5. Approvals (idempotent) -------------------------------------------
MAX = 2**256 - 1
def ensure_approvals():
    if usdc.functions.allowance(ME, NEG_RISK_ADAPTER).call() < 10**18:
        tx = usdc.functions.approve(NEG_RISK_ADAPTER, MAX).build_transaction({
            "from": ME, "nonce": w3.eth.get_transaction_count(ME),
            "maxFeePerGas":          w3.to_wei(100, "gwei"),
            "maxPriorityFeePerGas":  w3.to_wei(30,  "gwei"),
            "chainId": 137,
        })
        # signed = acct.sign_transaction(tx); w3.eth.send_raw_transaction(signed.raw_transaction)
        print("[would broadcast] USDC.e.approve(adapter)")
    # also need 1155 approval for merge/redeem/convert legs
    print("[would broadcast] CTF.setApprovalForAll(adapter, true)")

ensure_approvals()

# ---- 6. Pull a candidate event from Gamma --------------------------------
events = requests.get(
    "https://gamma-api.polymarket.com/events",
    params={"closed":"false","limit":50,"order":"volume24hr","ascending":"false"},
    timeout=15,
).json()
neg = next(e for e in events if e.get("negRisk") and e.get("markets"))
mkt = neg["markets"][0]
condition_id_hex = mkt["conditionId"]  # 0x...
print(f"event: {neg['title']!r}  conditionId: {condition_id_hex}")

# ---- 7. Simulate redeem on the YES leg of one binary ---------------------
# amounts MUST line up with outcome slot count (2 for binary): [yes_qty, no_qty]
SIZE = 1_000_000  # 1.0 USDC.e (6 dp); placeholder until balances are real
amounts = [SIZE, 0]

redeem_call = adapter.functions.redeemPositions(
    Web3.to_bytes(hexstr=condition_id_hex),
    amounts,
)

# eth_call simulation (no broadcast). Will revert if the market is unresolved
# or if you don't actually hold the tokens — both are expected for a dry run.
try:
    sim = redeem_call.call({"from": ME})
    print("simulated redeemPositions OK; return:", sim)
except Exception as exc:
    print("simulated redeemPositions reverted (expected for dry run):", exc)

# Gas estimate for a real broadcast:
try:
    gas = redeem_call.estimate_gas({"from": ME})
    print("gas estimate:", gas)
except Exception as exc:
    print("estimate_gas reverted (likely unresolved or no balance):", exc)
```

---

## 8. Gotchas

1. **USDC.e ≠ native USDC.** Polymarket exclusively uses **bridged USDC.e**
   `0x2791…84174`. Approving native Circle USDC `0x3c499…3359` will silently
   fail every order placement and adapter call. Confirm balance with
   `usdc.functions.symbol().call() == "USDC"` *and* address match.
2. **Two distinct allowances.** You must `approve(USDC.e → NegRiskAdapter)`
   *and* `setApprovalForAll(CTF → NegRiskAdapter, true)`. The second is
   needed for `mergePositions`, `redeemPositions`, and `convertPositions`
   because the adapter pulls your ERC-1155 outcome tokens before burning.
   Trading additionally requires the same two approvals targeting the
   **NegRiskCtfExchange** address.
3. **Gas estimates (Polygon, ~April 2026 baseline).** Approximate, varies
   ±30% with calldata size:
   - `splitPosition`        ~ 200–250 k gas
   - `mergePositions`       ~ 200–250 k gas
   - `redeemPositions(N=2)` ~ 150–220 k gas (single binary)
   - `convertPositions`     ~ 250–400 k gas (depends on `indexSet` popcount)
   At ~50 gwei `maxFeePerGas`, redeem costs roughly $0.005–$0.02 of MATIC.
   At Polygon gas spikes (>500 gwei) this can rise 10×; size the arb spread
   accordingly.
4. **Resolution dependency on UMA.** Redeem only works after the
   UmaCtfAdapter has called `reportPayouts` upstream and (for negRisk)
   the NegRiskOperator has called `reportOutcome`. Until then
   `redeemPositions` reverts with `MarketNotResolved`/payout-vector-empty.
   UMA's optimistic oracle has a **2-hour liveness window** (default) per
   question; disputes extend by days.
5. **No-winner / all-NO is an invalid state.** NegRisk *requires* exactly
   one question per market to resolve YES. Per the adapter docs and audit:
   if the oracle tries to report a second YES, `reportOutcome` reverts and
   the market is stuck pending manual operator action; if all questions go
   NO the system is "designed to prevent" that scenario but has no
   automatic refund path. Polymarket's stated stance after past disputes
   has been **no refunds for resolution disagreements**. Architect the
   strategy so you can hold or sell tokens before final resolution if
   ambiguity emerges.
   ([NegRisk docs](https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/docs/NegRiskAdapter.md),
   [Coindesk – UMA/Polymarket dispute, Mar 2025](https://www.coindesk.com/markets/2025/03/27/polymarket-uma-communities-lock-horns-after-usd7m-ukraine-bet-resolves))
6. **Operator-only `safeTransferFrom`.** The adapter's `safeTransferFrom`
   has an `onlyAdmin` modifier. Don't try to ERC-1155-transfer wrapped
   positions through the adapter; transfer directly through the underlying
   ConditionalTokens contract.
7. **`negRiskAugmented` events.** When `enableNegRisk` is true, new
   outcomes (questions) can be appended to a marketId after creation. Your
   "Σ asks" snapshot can become stale if a new candidate is added between
   detection and trade — re-pull the event before lifting offers.
8. **CLOB order flag.** When placing orders against negRisk markets, you
   must pass `neg_risk=True` in the order options of `py-clob-client` so
   the order is signed for the NegRiskCtfExchange (`0xC5d5…80a`) instead
   of the vanilla CTFExchange. Wrong exchange → orders rejected.
   ([NegRisk overview](https://docs.polymarket.com/developers/neg-risk/overview))

---

## Sources

- [Polymarket Contract Addresses](https://docs.polymarket.com/resources/contract-addresses)
- [Polymarket NegRisk Overview](https://docs.polymarket.com/developers/neg-risk/overview)
- [Polymarket CTF Overview](https://docs.polymarket.com/developers/CTF/overview)
- [neg-risk-ctf-adapter (repo)](https://github.com/Polymarket/neg-risk-ctf-adapter)
- [`NegRiskAdapter.sol`](https://raw.githubusercontent.com/Polymarket/neg-risk-ctf-adapter/main/src/NegRiskAdapter.sol)
- [`docs/NegRiskAdapter.md`](https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/docs/NegRiskAdapter.md)
- [`CTHelpers.sol`](https://raw.githubusercontent.com/Polymarket/neg-risk-ctf-adapter/main/src/libraries/CTHelpers.sol)
- [ctf-exchange (repo)](https://github.com/Polymarket/ctf-exchange)
- [ChainSecurity NegRiskAdapter audit (Apr 2024)](https://old.chainsecurity.com/wp-content/uploads/2024/04/ChainSecurity_Polymarket_NegRiskAdapter_audit.pdf)
- [Polymarket Resolution docs](https://docs.polymarket.com/concepts/resolution)
- [Coindesk: Polymarket/UMA Ukraine bet dispute (Mar 2025)](https://www.coindesk.com/markets/2025/03/27/polymarket-uma-communities-lock-horns-after-usd7m-ukraine-bet-resolves)
- PolygonScan verifications: [NegRiskAdapter](https://polygonscan.com/address/0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296), [NegRiskCtfExchange](https://polygonscan.com/address/0xc5d563a36ae78145c45a50134d48a1215220f80a), [ConditionalTokens](https://polygonscan.com/address/0x4d97dcd97ec945f40cf65f87097ace5ea0476045), [USDC.e](https://polygonscan.com/address/0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
