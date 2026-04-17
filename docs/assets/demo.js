/*
 * Live browser-side port of the Python arbitrage engine.
 *
 * What it does:
 *   1. Fetches a handful of active negRisk categorical events from Polymarket's
 *      public Gamma REST API (via a CORS proxy since Gamma doesn't send CORS
 *      headers; the WebSocket path below has no such restriction).
 *   2. Opens a WebSocket to the public CLOB market channel.
 *   3. Subscribes to every outcome token of those events.
 *   4. Maintains per-token L2 order book state (book snapshots + price_change deltas).
 *   5. On every update, recomputes Σ best-ask for the affected event; when it
 *      crosses below $1 net of an estimated fee+gas spread, fires an opportunity.
 *
 * This runs entirely in the browser. No backend, no keys, no state leaves the tab.
 */

// --- config ---------------------------------------------------------------

// Event list is published by a GitHub Action on schedule (see
// .github/workflows/refresh-events.yml). Loading it from the same origin
// sidesteps CORS on Polymarket's REST API. The WebSocket below streams live
// book data directly — it has no CORS restriction.
const EVENTS_URL = "./data/events.json";
const WS_URL     = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const MAX_EVENTS_TO_WATCH = 6;
const PING_INTERVAL_MS    = 10_000;
const FEE_BPS             = 0;
const EST_GAS_USD         = 0.10;
const EST_BASKET_SIZE     = 100;
const MIN_NET_EDGE_BPS    = 25;     // flash an opp when net edge >= 25 bps
const NEAR_ARB_THRESHOLD  = 0.01;   // show warn band when cost is within 1% of $1

// --- DOM ------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const connDot      = $("#conn-dot");
const connLabel    = $("#conn-label");
const picker       = $("#event-picker");
const eventTitle   = $("#event-title");
const eventSub     = $("#event-subtitle");
const basketCost   = $("#basket-cost");
const basketDelta  = $("#basket-delta");
const basketStatus = $("#basket-status");
const basketFill   = $("#basket-fill");
const outcomeList  = $("#outcome-list");
const sideEvents   = $("#stat-events");
const sideTokens   = $("#stat-tokens");
const sideBooks    = $("#stat-books");
const sideMsgs     = $("#stat-msgs");
const sideUptime   = $("#stat-uptime");
const oppsStream   = $("#opps-stream");
const oppsEmpty    = $("#opps-empty");

// --- state ----------------------------------------------------------------

const state = {
  events: {},
  books:  {},
  tokenToEvent: {},
  selectedEventId: null,
  msgCount: 0,
  oppCount: 0,
  startTime: Date.now(),
  ws: null,
  pingTimer: null,
  lastOppIdByEvent: {},
};

// Minimal sorted map for a price ladder.
class Ladder {
  constructor() { this.m = {}; this.sortedKeys = null; }
  set(price, size) {
    if (size <= 0) this.del(price);
    else { this.m[price] = size; this.sortedKeys = null; }
  }
  del(price) { delete this.m[price]; this.sortedKeys = null; }
  clear()    { this.m = {}; this.sortedKeys = null; }
  size()     { return Object.keys(this.m).length; }
  keys() {
    if (this.sortedKeys === null) {
      this.sortedKeys = Object.keys(this.m)
        .map(parseFloat).sort((a, b) => a - b);
    }
    return this.sortedKeys;
  }
  best(ascending) {
    const ks = this.keys();
    if (!ks.length) return null;
    const p = ascending ? ks[0] : ks[ks.length - 1];
    return { price: p, size: this.m[p] };
  }
}

// --- bootstrap -------------------------------------------------------------

bootstrap().catch((err) => {
  console.error("bootstrap failed", err);
  setConn("bad", "unable to load events — " + err.message);
});

async function bootstrap() {
  setConn("pend", "loading active Polymarket events…");
  const events = await fetchActiveNegRiskEvents();
  if (!events.length) {
    setConn("bad", "no active negRisk events found — Polymarket may be quiet");
    return;
  }
  for (const e of events) {
    state.events[e.id] = e;
    for (const o of e.outcomes) state.tokenToEvent[o.token_id] = e.id;
  }
  state.selectedEventId = events[0].id;
  sideEvents.textContent = Object.keys(state.events).length;
  sideTokens.textContent = Object.keys(state.tokenToEvent).length;
  renderEventPicker();
  renderSelectedEvent();
  startUptimeTicker();
  connectWS();
}

// --- REST fetch with proxy fallback ---------------------------------------

async function fetchActiveNegRiskEvents() {
  const url = new URL(GAMMA_URL);
  url.searchParams.set("closed", "false");
  url.searchParams.set("archived", "false");
  url.searchParams.set("active", "true");
  url.searchParams.set("limit", "25");
  url.searchParams.set("order", "volume24hr");
  url.searchParams.set("ascending", "false");

  let lastErr = null;
  let raw = null;
  for (const wrap of CORS_PROXIES) {
    try {
      const resp = await fetch(wrap(url.toString()));
      if (!resp.ok) { lastErr = new Error("Gamma /events " + resp.status); continue; }
      raw = await resp.json();
      break;
    } catch (e) { lastErr = e; }
  }
  if (raw === null) throw lastErr || new Error("All fetch strategies failed");
  if (!Array.isArray(raw)) return [];

  const kept = [];
  for (const ev of raw) {
    if (!ev.negRisk) continue;
    const markets = ev.markets || [];
    if (markets.length < 2) continue;
    const outcomes = [];
    for (let i = 0; i < markets.length; i++) {
      const m = markets[i];
      if (m.closed || m.archived) { outcomes.length = 0; break; }
      let ids = m.clobTokenIds;
      if (typeof ids === "string") {
        try { ids = JSON.parse(ids); } catch { ids = null; }
      }
      if (!Array.isArray(ids) || !ids.length) { outcomes.length = 0; break; }
      outcomes.push({
        token_id: String(ids[0]),
        name: m.groupItemTitle || m.outcome || m.question || ("Outcome " + (i + 1)),
      });
    }
    if (outcomes.length < 2) continue;
    kept.push({
      id: ev.negRiskMarketID || String(ev.id),
      title: ev.title || ev.slug || "Event",
      slug:  ev.slug,
      outcomes,
      sum: null,
      lastUpdate: 0,
    });
    if (kept.length >= MAX_EVENTS_TO_WATCH) break;
  }
  return kept;
}

// --- WebSocket ------------------------------------------------------------

function connectWS() {
  const tokenIds = Object.keys(state.tokenToEvent);
  if (!tokenIds.length) { setConn("bad", "no tokens to subscribe"); return; }
  setConn("pend", "opening WebSocket to CLOB…");

  const ws = new WebSocket(WS_URL);
  state.ws = ws;

  ws.addEventListener("open", () => {
    setConn("on", `subscribed to ${tokenIds.length} tokens across ${Object.keys(state.events).length} events`);
    ws.send(JSON.stringify({
      type: "market",
      assets_ids: tokenIds,
      custom_feature_enabled: true,
    }));
    if (state.pingTimer) clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => {
      try { ws.send("PING"); } catch { /* noop */ }
    }, PING_INTERVAL_MS);
  });

  ws.addEventListener("message", (ev) => {
    const raw = ev.data;
    if (typeof raw === "string") {
      const t = raw.trim();
      if (t === "PONG" || t === "PING") return;
      try { handlePayload(JSON.parse(t)); } catch { /* noop */ }
    }
  });

  ws.addEventListener("close", () => {
    setConn("bad", "WebSocket closed — reconnecting…");
    if (state.pingTimer) clearInterval(state.pingTimer);
    setTimeout(connectWS, 3000);
  });

  ws.addEventListener("error", () => setConn("bad", "WebSocket error"));
}

function handlePayload(payload) {
  if (Array.isArray(payload)) {
    for (const m of payload) dispatch(m);
  } else if (payload && typeof payload === "object") {
    dispatch(payload);
  }
}

function dispatch(msg) {
  state.msgCount++;
  sideMsgs.textContent = state.msgCount.toLocaleString();
  const t = msg.event_type;
  if (t === "book")          applyBookSnapshot(msg);
  else if (t === "price_change") applyPriceChange(msg);
}

function applyBookSnapshot(msg) {
  const assetId = msg.asset_id;
  if (!assetId || !state.tokenToEvent[assetId]) return;
  const book = getBook(assetId);
  book.bids.clear();
  book.asks.clear();
  if (Array.isArray(msg.bids)) {
    for (const lvl of msg.bids) {
      const p = parseFloat(lvl.price), s = parseFloat(lvl.size);
      if (p > 0 && s > 0) book.bids.set(p, s);
    }
  }
  if (Array.isArray(msg.asks)) {
    for (const lvl of msg.asks) {
      const p = parseFloat(lvl.price), s = parseFloat(lvl.size);
      if (p > 0 && s > 0) book.asks.set(p, s);
    }
  }
  onBookUpdate(assetId);
}

function applyPriceChange(msg) {
  if (!Array.isArray(msg.price_changes)) return;
  const touched = new Set();
  for (const c of msg.price_changes) {
    const assetId = c.asset_id;
    if (!assetId || !state.tokenToEvent[assetId]) continue;
    const p = parseFloat(c.price), s = parseFloat(c.size);
    if (!(p > 0) || isNaN(s)) continue;
    const book = getBook(assetId);
    const side = (c.side || "").toUpperCase();
    if (side === "BUY")       book.bids.set(p, s);
    else if (side === "SELL") book.asks.set(p, s);
    else continue;
    touched.add(assetId);
  }
  for (const id of touched) onBookUpdate(id);
}

function getBook(tokenId) {
  if (!state.books[tokenId]) {
    state.books[tokenId] = { bids: new Ladder(), asks: new Ladder() };
    sideBooks.textContent = Object.keys(state.books).length.toLocaleString();
  }
  return state.books[tokenId];
}

// --- engine ---------------------------------------------------------------

function onBookUpdate(tokenId) {
  const eventId = state.tokenToEvent[tokenId];
  if (!eventId) return;
  evaluateEvent(eventId);
  if (eventId === state.selectedEventId) renderSelectedEvent();
  renderEventPicker(); // pill label updates live
}

function evaluateEvent(eventId) {
  const ev = state.events[eventId];
  if (!ev) return;
  let sum = 0, missing = 0;
  for (const o of ev.outcomes) {
    const b = state.books[o.token_id];
    const best = b && b.asks.best(true);
    if (!best) { missing += 1; continue; }
    sum += best.price;
  }
  if (missing > 0) { ev.sum = null; return; }
  ev.sum = sum;
  ev.lastUpdate = Date.now();

  const grossPerBasket = 1 - sum;
  const gasAmortized   = EST_GAS_USD / EST_BASKET_SIZE;
  const feeCost        = (FEE_BPS / 10_000) * sum;
  const netPerBasket   = grossPerBasket - feeCost - gasAmortized;
  const netBps         = Math.round(netPerBasket * 10_000);

  if (netBps >= MIN_NET_EDGE_BPS) pushOpportunity(ev, sum, netBps);
}

function pushOpportunity(ev, sum, netBps) {
  const prev = state.lastOppIdByEvent[ev.id];
  if (prev && (Date.now() - prev.at < 2500) && Math.abs(prev.bps - netBps) < 5) return;
  state.lastOppIdByEvent[ev.id] = { at: Date.now(), bps: netBps };
  state.oppCount += 1;
  oppsEmpty.style.display = "none";

  const div = document.createElement("div");
  div.className = "opp-alert";
  div.innerHTML = `
    <div class="when">${new Date().toLocaleTimeString()}</div>
    <div class="title">${escapeHtml(ev.title)}</div>
    <div class="detail">basket $${sum.toFixed(4)} · edge ${netBps >= 0 ? "+" : ""}${netBps} bps</div>
  `;
  oppsStream.prepend(div);
  while (oppsStream.childNodes.length > 12) oppsStream.removeChild(oppsStream.lastChild);
}

// --- rendering ------------------------------------------------------------

function setConn(cls, text) {
  connDot.className = "dot " + cls;
  connLabel.textContent = text;
}

function renderEventPicker() {
  picker.innerHTML = "";
  for (const id of Object.keys(state.events)) {
    const ev = state.events[id];
    const pill = document.createElement("button");
    const cls = classForSum(ev.sum);
    pill.className = "event-pill" + (id === state.selectedEventId ? " active" : "")
                    + (cls === "arb" ? " has-arb" : cls === "near" ? " near-arb" : "");
    pill.innerHTML = `
      <span>${escapeHtml(truncate(ev.title, 36))}</span>
      <span class="pill-cost">${ev.sum === null ? "—" : "$" + ev.sum.toFixed(3)}</span>
    `;
    pill.addEventListener("click", () => {
      state.selectedEventId = id;
      renderEventPicker();
      renderSelectedEvent();
    });
    picker.appendChild(pill);
  }
}

function renderSelectedEvent() {
  const ev = state.events[state.selectedEventId];
  if (!ev) return;
  eventTitle.textContent = ev.title;
  eventSub.textContent = `${ev.outcomes.length} outcomes · one will win, all others pay $0`;

  // Collect best-asks
  let sum = 0, complete = true;
  const rows = [];
  for (const o of ev.outcomes) {
    const b = state.books[o.token_id];
    const best = b && b.asks.best(true);
    if (!best) { complete = false; rows.push({ ...o, price: null, size: null }); }
    else { sum += best.price; rows.push({ ...o, price: best.price, size: best.size }); }
  }
  // Sort outcomes by price descending (most likely outcome first)
  rows.sort((a, b) => (b.price ?? -1) - (a.price ?? -1));

  renderBasketMeter(complete ? sum : null);
  renderOutcomeList(rows, complete ? sum : null);
}

function renderBasketMeter(sum) {
  if (sum === null) {
    basketCost.textContent = "—";
    basketCost.className = "big-val";
    basketDelta.textContent = "waiting for every outcome's book…";
    basketDelta.className = "delta fair";
    basketStatus.textContent = "loading";
    basketStatus.className = "status fair";
    basketFill.style.width = "0%";
    basketFill.className = "fill";
    return;
  }
  const delta = sum - 1;
  const cls = classForSum(sum);

  basketCost.textContent = "$" + sum.toFixed(4);
  basketCost.className = "big-val" + (cls === "arb" ? " arb" : cls === "near" ? " near" : "");

  const sign = delta >= 0 ? "+" : "";
  const bps = Math.round(delta * 10_000);
  basketDelta.textContent = (cls === "arb"
    ? `${sign}$${delta.toFixed(4)} below $1 · arbitrage of ${Math.abs(bps)} bps`
    : cls === "near"
    ? `${sign}$${delta.toFixed(4)} above $1 · no arbitrage yet (${bps} bps over)`
    : `${sign}$${delta.toFixed(4)} above $1 · fair pricing, no arbitrage`);
  basketDelta.className = "delta " + cls;

  basketStatus.textContent = cls === "arb" ? "ARBITRAGE" : cls === "near" ? "NEAR MISS" : "FAIR";
  basketStatus.className = "status " + cls;

  // Map cost $0.80 → 0%, $1.00 → 50%, $1.20 → 100%
  const pct = Math.max(0, Math.min(100, ((sum - 0.80) / 0.40) * 100));
  basketFill.style.width = pct.toFixed(1) + "%";
  basketFill.className = "fill" + (cls === "arb" ? " arb" : cls === "near" ? " near" : "");
}

function renderOutcomeList(rows, totalSum) {
  outcomeList.innerHTML = "";
  for (const r of rows) {
    const row = document.createElement("div");
    const hasPrice = r.price !== null;
    const pct = hasPrice ? (r.price * 100) : 0;
    row.className = "outcome-row" + (!hasPrice ? " empty" : "")
                  + (hasPrice && totalSum !== null && totalSum < 1 && classForSum(totalSum) === "arb" ? " highlighted" : "");
    const name = escapeHtml(r.name);
    const pctText = hasPrice ? pct.toFixed(1) + "%" : "waiting";
    const priceSize = hasPrice
      ? `$${r.price.toFixed(4)} · ${formatSize(r.size)} shares`
      : "no asks yet";
    row.innerHTML = `
      <div class="left">
        <div class="outcome-name">${name}</div>
        <div class="prob-bar"><div class="prob-fill" style="width: ${Math.min(100, pct).toFixed(1)}%"></div></div>
      </div>
      <div class="right">
        <div class="pct">${pctText}</div>
        <div class="price-size">${priceSize}</div>
      </div>
    `;
    outcomeList.appendChild(row);
  }
}

function classForSum(sum) {
  if (sum === null || sum === undefined) return "fair";
  if (sum < 1.0) return "arb";
  if (sum <= 1.0 + NEAR_ARB_THRESHOLD) return "near";
  return "fair";
}

function startUptimeTicker() {
  setInterval(() => {
    const secs = Math.floor((Date.now() - state.startTime) / 1000);
    const mm = String(Math.floor(secs / 60)).padStart(2, "0");
    const ss = String(secs % 60).padStart(2, "0");
    sideUptime.textContent = `${mm}:${ss}`;
  }, 1000);
}

// --- utils ----------------------------------------------------------------

function formatSize(s) {
  if (s >= 1_000_000) return (s / 1_000_000).toFixed(2) + "M";
  if (s >= 10_000)    return (s / 1_000).toFixed(1) + "k";
  if (s >= 1_000)     return (s / 1_000).toFixed(2) + "k";
  return s.toFixed(0);
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}
function truncate(s, n) {
  s = String(s);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
