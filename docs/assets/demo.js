/*
 * Live browser-side port of the Python arbitrage engine.
 *
 * What it does:
 *   1. Fetches a handful of active negRisk categorical events from Polymarket's
 *      public Gamma REST API.
 *   2. Opens a WebSocket to the public CLOB market channel.
 *   3. Subscribes to all outcome token ids for the selected events.
 *   4. Maintains per-token L2 order book state (book snapshots + price_change deltas).
 *   5. On every update, recomputes Σ best-ask for the affected event; when it
 *      crosses below $1 net of an estimated fee+gas spread, fires an opportunity.
 *
 * This runs entirely in the browser. No backend, no keys, no state leaves the tab.
 */

const GAMMA_URL = "https://gamma-api.polymarket.com/events";
// Gamma API does not send CORS headers, so browser fetches from github.io are
// blocked. Try direct first, then fall back to a public proxy. allorigins is
// preferred because corsproxy.io has a 1MB body limit and Gamma's /events
// payload with full market bodies is ~8MB.
const CORS_PROXIES = [
  (u) => u,                                                              // direct
  (u) => "https://api.allorigins.win/raw?url=" + encodeURIComponent(u),  // big payloads ok
  (u) => "https://corsproxy.io/?" + encodeURIComponent(u),               // last resort
];
const WS_URL    = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const MAX_EVENTS_TO_WATCH = 6;
const PING_INTERVAL_MS    = 10_000;
const FEE_BPS             = 0;          // Polymarket taker fee is currently 0
const EST_GAS_USD         = 0.10;       // estimated gas per basket
const EST_BASKET_SIZE     = 100;        // for gas amortisation display only
const MIN_NET_EDGE_BPS    = 25;         // dashboard threshold (wider than prod)

// --- DOM shortcuts ---------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const eventList     = $("#event-list");
const bookWrap      = $("#book-wrap");
const selectedTitle = $("#selected-title");
const basketSummary = $("#basket-summary");
const oppsStream    = $("#opps-stream");
const oppsEmpty     = $("#opps-empty");
const connDot       = $("#conn-dot");
const connLabel     = $("#conn-label");
const statMsgs      = $("#stat-msgs");
const statBooks     = $("#stat-books");
const statOpps      = $("#stat-opps");
const statUptime    = $("#stat-uptime");

// --- state -----------------------------------------------------------------

const state = {
  events: {},             // event_id -> {id, title, outcomes: [{token_id, name}], sum, lastUpdate}
  books:  {},             // token_id -> {bids: SortedMap, asks: SortedMap, lastHash, updated}
  tokenToEvent: {},       // token_id -> event_id
  selectedEventId: null,
  msgCount: 0,
  oppCount: 0,
  startTime: Date.now(),
  ws: null,
  pingTimer: null,
  lastOppIdByEvent: {},   // dedupe flashing the same opp repeatedly
};

// Tiny sorted map (price -> size), good enough for the demo.
// Uses a plain object + cached sorted keys; re-sorts on insert/delete.
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
  vwap(targetSize) {
    if (targetSize <= 0 || !this.size()) return null;
    let remaining = targetSize, cost = 0, filled = 0, levels = 0;
    for (const p of this.keys()) {
      const sz = this.m[p];
      if (!sz) continue;
      const take = Math.min(remaining, sz);
      cost += take * p;
      filled += take;
      levels += 1;
      remaining -= take;
      if (remaining <= 0) break;
    }
    if (filled <= 0) return null;
    return { vwap: cost / filled, filled, levels };
  }
}

// --- bootstrap -------------------------------------------------------------

bootstrap().catch(err => {
  console.error("bootstrap failed", err);
  setConn("bad", "unable to load events — " + err.message);
});

async function bootstrap() {
  setConn("pend", "fetching events from Gamma…");
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
  renderEventList();
  renderSelectedBooks();
  startUptimeTicker();
  connectWS();
}

// --- REST: pull active negRisk events --------------------------------------

async function fetchActiveNegRiskEvents() {
  // Pull the first page by 24h volume, then filter client-side.
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
    } catch (e) {
      lastErr = e;
    }
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

// --- WebSocket -------------------------------------------------------------

function connectWS() {
  const tokenIds = Object.keys(state.tokenToEvent);
  if (!tokenIds.length) { setConn("bad", "no tokens to subscribe"); return; }
  setConn("pend", "opening WebSocket…");

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
      try {
        const payload = JSON.parse(t);
        handlePayload(payload);
      } catch { /* ignore malformed */ }
    }
  });

  ws.addEventListener("close", () => {
    setConn("bad", "WebSocket closed — reconnecting in 3s");
    if (state.pingTimer) clearInterval(state.pingTimer);
    setTimeout(connectWS, 3000);
  });

  ws.addEventListener("error", () => {
    setConn("bad", "WebSocket error");
  });
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
  statMsgs.textContent = state.msgCount.toLocaleString();

  const t = msg.event_type;
  if (t === "book")          applyBookSnapshot(msg);
  else if (t === "price_change") applyPriceChange(msg);
  // tick_size_change / last_trade_price / best_bid_ask ignored for the demo
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
  book.updated = Date.now();
  book.lastHash = msg.hash;
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
    book.updated = Date.now();
    if (c.hash) book.lastHash = c.hash;
    touched.add(assetId);
  }
  for (const id of touched) onBookUpdate(id);
}

function getBook(tokenId) {
  if (!state.books[tokenId]) {
    state.books[tokenId] = { bids: new Ladder(), asks: new Ladder(),
                             updated: 0, lastHash: null };
    statBooks.textContent = Object.keys(state.books).length.toLocaleString();
  }
  return state.books[tokenId];
}

// --- engine: recompute basket math -----------------------------------------

function onBookUpdate(tokenId) {
  const eventId = state.tokenToEvent[tokenId];
  if (!eventId) return;
  evaluateEvent(eventId);
  if (eventId === state.selectedEventId) renderSelectedBooks();
  renderEventList();   // cheap enough to redo on every update
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
  // Dedupe if we just fired one for this event at a similar level.
  if (prev && (Date.now() - prev.at < 2500) && Math.abs(prev.bps - netBps) < 5) return;
  state.lastOppIdByEvent[ev.id] = { at: Date.now(), bps: netBps };
  state.oppCount += 1;
  statOpps.textContent = state.oppCount.toLocaleString();
  oppsEmpty.style.display = "none";

  const div = document.createElement("div");
  div.className = "opp-alert";
  div.innerHTML = `
    <div class="when">${new Date().toLocaleTimeString()}</div>
    <div class="headline">${escapeHtml(ev.title)}</div>
    <div>Σ best-ask = <strong>$${sum.toFixed(4)}</strong> · net edge ≈ <strong>${netBps} bps</strong></div>
  `;
  oppsStream.prepend(div);
  // Keep the list bounded.
  while (oppsStream.childNodes.length > 20) oppsStream.removeChild(oppsStream.lastChild);
}

// --- rendering -------------------------------------------------------------

function setConn(cls, text) {
  connDot.className = "status-dot " + cls;
  connLabel.textContent = text;
}

function renderEventList() {
  eventList.innerHTML = "";
  const ids = Object.keys(state.events);
  for (const id of ids) {
    const ev = state.events[id];
    const card = document.createElement("div");
    card.className = "event-card" + (id === state.selectedEventId ? " active" : "");
    const sumCls = ev.sum === null ? "neu"
                 : ev.sum < 1.00 ? "pos"
                 : "neg";
    const sumText = ev.sum === null ? "—" : "Σ " + ev.sum.toFixed(4);
    card.innerHTML = `
      <div class="title">${escapeHtml(ev.title)}</div>
      <div class="meta">${ev.outcomes.length} outcomes</div>
      <div class="sum ${sumCls}">${sumText}</div>
    `;
    card.addEventListener("click", () => {
      state.selectedEventId = id;
      renderEventList();
      renderSelectedBooks();
    });
    eventList.appendChild(card);
  }
}

function renderSelectedBooks() {
  const ev = state.events[state.selectedEventId];
  if (!ev) { selectedTitle.textContent = "—"; bookWrap.innerHTML = ""; return; }
  selectedTitle.textContent = ev.title;

  bookWrap.innerHTML = "";
  let sumOk = 0, any = 0;
  for (const o of ev.outcomes) {
    const b = state.books[o.token_id];
    const best = b && b.asks.best(true);
    any = (best ? (any + 1) : any);
    if (best) sumOk += best.price;
    const card = document.createElement("div");
    card.className = "outcome";
    if (best) {
      card.innerHTML = `
        <div class="name">${escapeHtml(o.name)}</div>
        <div class="best-ask">$${best.price.toFixed(4)}</div>
        <div class="size">ask size: ${formatSize(best.size)}</div>
      `;
    } else {
      card.innerHTML = `
        <div class="name">${escapeHtml(o.name)}</div>
        <div class="best-ask empty">no asks yet</div>
      `;
    }
    bookWrap.appendChild(card);
  }

  if (any === ev.outcomes.length) {
    const delta = 1 - sumOk;
    const bps = Math.round(delta * 10_000);
    const cls = delta > 0 ? "pos" : (delta < 0 ? "neg" : "neu");
    basketSummary.innerHTML = `
      Basket cost (1 share of each outcome at top of book):
      <strong>$${sumOk.toFixed(4)}</strong>
      · gross edge vs. $1 guarantee:
      <span style="color: var(--${cls === "pos" ? "pos" : cls === "neg" ? "neg" : "muted"}); font-family: var(--mono);">
        ${delta >= 0 ? "+" : ""}${delta.toFixed(4)} (${bps >= 0 ? "+" : ""}${bps} bps)
      </span>
      <span class="muted">· estimated gas ≈ $${EST_GAS_USD.toFixed(2)} amortised across a ${EST_BASKET_SIZE}-share basket</span>
    `;
  } else {
    basketSummary.textContent = `Waiting on ${ev.outcomes.length - any} of ${ev.outcomes.length} books…`;
  }
}

function startUptimeTicker() {
  setInterval(() => {
    const secs = Math.floor((Date.now() - state.startTime) / 1000);
    const mm = String(Math.floor(secs / 60)).padStart(2, "0");
    const ss = String(secs % 60).padStart(2, "0");
    statUptime.textContent = `${mm}:${ss}`;
  }, 1000);
}

// --- utils -----------------------------------------------------------------

function formatSize(s) {
  if (s >= 10_000) return (s / 1000).toFixed(1) + "k";
  if (s >= 1_000)  return (s / 1000).toFixed(2) + "k";
  return s.toFixed(0);
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}
