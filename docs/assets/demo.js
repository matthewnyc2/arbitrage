/*
 * Live Polymarket arbitrage scanner — portfolio demo.
 *
 * What this does, in plain English:
 *   - Loads a list of 6–8 currently-active categorical Polymarket events
 *     from a static JSON file that a GitHub Action refreshes every hour.
 *   - Opens a WebSocket to Polymarket's public CLOB and subscribes to every
 *     outcome token of those events.
 *   - Maintains order book state per outcome and recomputes "basket cost" —
 *     the total cost of buying one share of every answer — on every update.
 *   - If that basket cost drops below $1.00, it's a risk-free arbitrage and
 *     the page flashes green.
 *
 * This is a browser-only port of the Python engine in the repo. No backend,
 * no keys, no orders submitted.
 */

const EVENTS_URL = "./data/events.json";
const WS_URL     = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const MAX_EVENTS_TO_WATCH = 6;
const PING_INTERVAL_MS    = 10_000;
const NEAR_ARB_THRESHOLD  = 0.01;     // $0.01 above $1 counts as "near miss"

// --- DOM shortcuts ---------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

const el = {
  connDot:      $("#conn-dot"),
  connLabel:    $("#conn-label"),
  aLine:        $("#a-line"),
  aSub:         $("#a-sub"),
  watchCount:   $("#watch-count"),
  eventList:    $("#event-list"),
  eventTitle:   $("#event-title"),
  eventSub:     $("#event-subtitle"),
  whyBtn:       $("#why-this-event"),
  basketCost:   $("#basket-cost"),
  basketCostSub:$("#basket-cost-sub"),
  basketPnl:    $("#basket-pnl"),
  basketPnlSub: $("#basket-pnl-sub"),
  basketStatus: $("#basket-status"),
  basketFill:   $("#basket-fill"),
  scaleButtons: $("#scale-buttons"),
  scaleOut:     $("#scale-out"),
  outcomeList:  $("#outcome-list"),
  statEvents:   $("#stat-events"),
  statTokens:   $("#stat-tokens"),
  statBooks:    $("#stat-books"),
  statMsgs:     $("#stat-msgs"),
  statUptime:   $("#stat-uptime"),
  modal:        $("#modal"),
  modalContent: $("#modal-content"),
};

// --- state ----------------------------------------------------------------

const state = {
  events: {},
  books:  {},
  tokenToEvent: {},
  selectedEventId: null,
  msgCount: 0,
  startTime: Date.now(),
  ws: null,
  pingTimer: null,
  scale: 1,
};

class Ladder {
  constructor() { this.m = {}; this.sortedKeys = null; }
  set(price, size) { if (size <= 0) this.del(price); else { this.m[price] = size; this.sortedKeys = null; } }
  del(price) { delete this.m[price]; this.sortedKeys = null; }
  clear()    { this.m = {}; this.sortedKeys = null; }
  size()     { return Object.keys(this.m).length; }
  keys()     { if (this.sortedKeys === null) this.sortedKeys = Object.keys(this.m).map(parseFloat).sort((a,b)=>a-b); return this.sortedKeys; }
  best(ascending) {
    const ks = this.keys();
    if (!ks.length) return null;
    const p = ascending ? ks[0] : ks[ks.length - 1];
    return { price: p, size: this.m[p] };
  }
}

// --- bootstrap ------------------------------------------------------------

bootstrap().catch((err) => {
  console.error("bootstrap failed", err);
  setConn("bad", "unable to load events — " + err.message);
  el.aLine.innerHTML = `<span class="verdict no">Couldn't load.</span>`;
  el.aSub.textContent = "Refresh the page in a minute — the events list refreshes hourly.";
});

async function bootstrap() {
  setConn("pend", "loading active events…");
  const events = await fetchActiveNegRiskEvents();
  if (!events.length) {
    setConn("bad", "no active events found");
    return;
  }
  for (const e of events) {
    state.events[e.id] = e;
    for (const o of e.outcomes) state.tokenToEvent[o.token_id] = e.id;
  }
  state.selectedEventId = events[0].id;
  el.watchCount.textContent = events.length;
  el.statEvents.textContent = events.length;
  el.statTokens.textContent = Object.keys(state.tokenToEvent).length;
  renderEventList();
  renderSelectedEvent();
  wireInteractions();
  startUptimeTicker();
  connectWS();
}

// --- REST (same-origin events.json — GitHub Action refreshes hourly) ------

async function fetchActiveNegRiskEvents() {
  const resp = await fetch(EVENTS_URL + "?t=" + Date.now());
  if (!resp.ok) throw new Error("events.json " + resp.status);
  const payload = await resp.json();
  const raw = Array.isArray(payload?.events) ? payload.events : [];
  const kept = [];
  for (const ev of raw) {
    if (!Array.isArray(ev.outcomes) || ev.outcomes.length < 2) continue;
    kept.push({
      id: String(ev.id),
      title: String(ev.title || "Event"),
      slug:  ev.slug,
      outcomes: ev.outcomes.map(o => ({ token_id: String(o.token_id), name: String(o.name) })),
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
  if (!tokenIds.length) { setConn("bad", "no tokens"); return; }
  setConn("pend", "opening connection to Polymarket…");

  const ws = new WebSocket(WS_URL);
  state.ws = ws;

  ws.addEventListener("open", () => {
    setConn("on", `live · reading order books for ${Object.keys(state.events).length} events`);
    ws.send(JSON.stringify({ type: "market", assets_ids: tokenIds, custom_feature_enabled: true }));
    if (state.pingTimer) clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => { try { ws.send("PING"); } catch {} }, PING_INTERVAL_MS);
  });

  ws.addEventListener("message", (ev) => {
    const raw = ev.data;
    if (typeof raw === "string") {
      const t = raw.trim();
      if (t === "PONG" || t === "PING") return;
      try { handlePayload(JSON.parse(t)); } catch {}
    }
  });

  ws.addEventListener("close", () => {
    setConn("bad", "connection dropped — reconnecting…");
    if (state.pingTimer) clearInterval(state.pingTimer);
    setTimeout(connectWS, 3000);
  });

  ws.addEventListener("error", () => setConn("bad", "connection error"));
}

function handlePayload(payload) {
  if (Array.isArray(payload)) for (const m of payload) dispatch(m);
  else if (payload && typeof payload === "object") dispatch(payload);
}

function dispatch(msg) {
  state.msgCount++;
  el.statMsgs.textContent = state.msgCount.toLocaleString();
  const t = msg.event_type;
  if (t === "book")          applyBookSnapshot(msg);
  else if (t === "price_change") applyPriceChange(msg);
}

function applyBookSnapshot(msg) {
  const assetId = msg.asset_id;
  if (!assetId || !state.tokenToEvent[assetId]) return;
  const book = getBook(assetId);
  book.bids.clear(); book.asks.clear();
  for (const lvl of (msg.bids || [])) {
    const p = parseFloat(lvl.price), s = parseFloat(lvl.size);
    if (p > 0 && s > 0) book.bids.set(p, s);
  }
  for (const lvl of (msg.asks || [])) {
    const p = parseFloat(lvl.price), s = parseFloat(lvl.size);
    if (p > 0 && s > 0) book.asks.set(p, s);
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
    el.statBooks.textContent = Object.keys(state.books).length.toLocaleString();
  }
  return state.books[tokenId];
}

// --- engine ---------------------------------------------------------------

function onBookUpdate(tokenId) {
  const eventId = state.tokenToEvent[tokenId];
  if (!eventId) return;
  evaluateEvent(eventId);
  renderEventList();
  if (eventId === state.selectedEventId) renderSelectedEvent();
  renderHeroAnswer();
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
  ev.sum = missing > 0 ? null : sum;
  ev.lastUpdate = Date.now();
}

// --- rendering ------------------------------------------------------------

function setConn(cls, text) {
  el.connDot.className = "dot " + cls;
  el.connLabel.textContent = text;
}

function renderHeroAnswer() {
  // Find the cheapest basket across all events
  let bestEv = null, bestSum = Infinity;
  for (const id of Object.keys(state.events)) {
    const ev = state.events[id];
    if (ev.sum === null || ev.sum === undefined) continue;
    if (ev.sum < bestSum) { bestSum = ev.sum; bestEv = ev; }
  }
  if (!bestEv) {
    el.aLine.innerHTML = `<span class="spinner"></span><span class="muted">checking live prices…</span>`;
    return;
  }
  const cls = classForSum(bestSum);
  const gap = bestSum - 1;
  if (cls === "arb") {
    const bps = Math.round(-gap * 10_000);
    el.aLine.innerHTML = `<span class="verdict yes">Yes!</span> <span class="detail">The cheapest bundle is</span> <span class="cost-chip yes">$${bestSum.toFixed(4)}</span><span class="detail">— free ${Math.abs(bps)} bps (${(-gap*100).toFixed(2)}¢) per $1 bet</span>`;
    el.aSub.innerHTML = `On <strong>${escapeHtml(bestEv.title)}</strong>. Click it on the left to see the details.`;
  } else if (cls === "near") {
    el.aLine.innerHTML = `<span class="verdict near">Almost.</span> <span class="detail">The cheapest bundle is</span> <span class="cost-chip near">$${bestSum.toFixed(4)}</span><span class="detail">— you'd lose ${(gap*100).toFixed(2)}¢ per $1 bet</span>`;
    el.aSub.innerHTML = `On <strong>${escapeHtml(bestEv.title)}</strong>. Watch it — if another trader sells off this might flip into arbitrage.`;
  } else {
    el.aLine.innerHTML = `<span class="verdict no">Not right now.</span> <span class="detail">The cheapest bundle is</span> <span class="cost-chip no">$${bestSum.toFixed(4)}</span><span class="detail">— you'd lose ${(gap*100).toFixed(2)}¢ per $1 bet</span>`;
    el.aSub.innerHTML = `Watching <strong>${Object.keys(state.events).length} events</strong>. This is the normal state — arbitrage windows are rare.`;
  }
}

function renderEventList() {
  el.eventList.innerHTML = "";
  for (const id of Object.keys(state.events)) {
    const ev = state.events[id];
    const cls = classForSum(ev.sum);
    const costText = ev.sum === null ? "waiting" : "$" + ev.sum.toFixed(3);
    const costCls = ev.sum === null ? "" : cls === "arb" ? "yes" : cls === "near" ? "near" : "no";
    const card = document.createElement("div");
    card.className = "event-card" + (id === state.selectedEventId ? " active" : "");
    card.innerHTML = `
      <div class="ec-title">${escapeHtml(ev.title)}</div>
      <div class="ec-meta">
        <span class="ec-count">${ev.outcomes.length} possible answers</span>
        <span class="ec-cost ${costCls}">${costText}</span>
      </div>
    `;
    card.addEventListener("click", () => {
      state.selectedEventId = id;
      renderEventList();
      renderSelectedEvent();
    });
    el.eventList.appendChild(card);
  }
}

function renderSelectedEvent() {
  const ev = state.events[state.selectedEventId];
  if (!ev) return;
  el.eventTitle.textContent = ev.title;
  el.eventSub.textContent = `${ev.outcomes.length} possible answers · exactly one will win and pay $1`;

  let sum = 0, complete = true;
  const rows = [];
  for (const o of ev.outcomes) {
    const b = state.books[o.token_id];
    const best = b && b.asks.best(true);
    if (!best) { complete = false; rows.push({ ...o, price: null, size: null }); }
    else { sum += best.price; rows.push({ ...o, price: best.price, size: best.size }); }
  }
  rows.sort((a, b) => (b.price ?? -1) - (a.price ?? -1));

  renderCalcCard(complete ? sum : null);
  renderOutcomeList(rows, complete ? sum : null);
}

function renderCalcCard(sum) {
  if (sum === null) {
    el.basketCost.textContent = "—";
    el.basketCost.className = "cell-val";
    el.basketCostSub.textContent = "waiting for every answer's order book…";
    el.basketPnl.textContent = "—";
    el.basketPnl.className = "cell-val";
    el.basketPnlSub.textContent = "per one-set bet";
    el.basketStatus.textContent = "loading";
    el.basketStatus.className = "status fair";
    el.basketFill.style.width = "0%";
    el.basketFill.className = "fill";
    el.scaleOut.innerHTML = "";
    return;
  }
  const cls = classForSum(sum);
  const delta = sum - 1;

  // scale
  const q = state.scale;
  const totalCost = sum * q;
  const totalGet  = 1 * q;
  const pnl       = totalGet - totalCost;

  el.basketCost.textContent = "$" + sum.toFixed(4);
  el.basketCost.className = "cell-val";
  el.basketCostSub.textContent = q === 1 ? "for one full set" : `× ${q} sets = $${totalCost.toFixed(2)} total`;

  el.basketPnl.textContent = (pnl >= 0 ? "+" : "") + "$" + pnl.toFixed(q >= 100 ? 2 : 4);
  el.basketPnl.className = "cell-val " + (cls === "arb" ? "pos" : cls === "near" ? "near" : "neg");
  el.basketPnlSub.textContent = q === 1
    ? (cls === "arb" ? "guaranteed — buy + redeem" : cls === "near" ? "you'd lose a tiny bit" : "you'd lose this no matter what")
    : `on a ${q}-set bet`;

  el.basketStatus.textContent = cls === "arb" ? "ARBITRAGE" : cls === "near" ? "NEAR MISS" : "NO ARB";
  el.basketStatus.className = "status " + cls;

  // interpretation line
  if (cls === "arb") {
    el.scaleOut.innerHTML = `
      Pay <strong>$${totalCost.toFixed(2)}</strong>,
      receive <strong>$${totalGet.toFixed(2)}</strong> when the event resolves,
      pocket <span class="gain">+$${pnl.toFixed(2)}</span> risk-free.
      Every leg fills and the winning outcome pays $1.
    `;
  } else {
    const perSet = delta;
    el.scaleOut.innerHTML = `
      Pay <strong>$${totalCost.toFixed(2)}</strong>,
      receive <strong>$${totalGet.toFixed(2)}</strong> when the event resolves,
      net <span class="loss">−$${Math.abs(pnl).toFixed(2)}</span>.
      That's ${(perSet*100).toFixed(2)}¢ too expensive per set — no arbitrage.
    `;
  }

  const pct = Math.max(0, Math.min(100, ((sum - 0.80) / 0.40) * 100));
  el.basketFill.style.width = pct.toFixed(1) + "%";
  el.basketFill.className = "fill" + (cls === "arb" ? " arb" : cls === "near" ? " near" : "");
}

function renderOutcomeList(rows, totalSum) {
  el.outcomeList.innerHTML = "";
  for (const r of rows) {
    const hasPrice = r.price !== null;
    const pct = hasPrice ? (r.price * 100) : 0;
    const highlighted = hasPrice && totalSum !== null && classForSum(totalSum) === "arb";
    const row = document.createElement("div");
    row.className = "outcome-row" + (!hasPrice ? " empty" : "") + (highlighted ? " highlighted" : "");
    row.innerHTML = `
      <div class="left">
        <div class="outcome-name">${escapeHtml(r.name)}</div>
        <div class="prob-bar"><div class="prob-fill" style="width: ${Math.min(100, pct).toFixed(1)}%"></div></div>
      </div>
      <div class="right">
        <div class="pct">${hasPrice ? pct.toFixed(1) + "%" : "waiting"}</div>
        <div class="price-size">${hasPrice ? `$${r.price.toFixed(4)} · ${formatSize(r.size)} available` : "no offers yet"}</div>
      </div>
    `;
    el.outcomeList.appendChild(row);
  }
}

function classForSum(sum) {
  if (sum === null || sum === undefined) return "fair";
  if (sum < 1.0) return "arb";
  if (sum <= 1.0 + NEAR_ARB_THRESHOLD) return "near";
  return "fair";
}

// --- interactions ---------------------------------------------------------

function wireInteractions() {
  // scale buttons
  el.scaleButtons.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-qty]");
    if (!btn) return;
    state.scale = parseInt(btn.dataset.qty, 10) || 1;
    for (const b of el.scaleButtons.querySelectorAll("button")) b.classList.toggle("active", b === btn);
    renderSelectedEvent();
  });

  // "what is this event" modal
  el.whyBtn.addEventListener("click", () => {
    const ev = state.events[state.selectedEventId];
    if (!ev) return;
    const pmUrl = ev.slug ? `https://polymarket.com/event/${encodeURIComponent(ev.slug)}` : "https://polymarket.com";
    el.modalContent.innerHTML = `
      <h3>${escapeHtml(ev.title)}</h3>
      <p>
        This is a real, currently-open question on Polymarket. There are
        <strong>${ev.outcomes.length} possible answers</strong>, and when the real
        event resolves, one will be declared the winner. Shares of the winning
        answer pay $1. Shares of every losing answer pay $0.
      </p>
      <p>
        The numbers on this page come straight from Polymarket's live order book —
        same feed their website uses. See the original market here:
      </p>
      <p><a href="${pmUrl}" target="_blank" rel="noopener">View on polymarket.com →</a></p>
    `;
    el.modal.hidden = false;
  });

  // modal close
  el.modal.addEventListener("click", (e) => {
    if (e.target.dataset?.close !== undefined) el.modal.hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") el.modal.hidden = true;
  });
}

function startUptimeTicker() {
  setInterval(() => {
    const secs = Math.floor((Date.now() - state.startTime) / 1000);
    const mm = String(Math.floor(secs / 60)).padStart(2, "0");
    const ss = String(secs % 60).padStart(2, "0");
    el.statUptime.textContent = `${mm}:${ss}`;
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
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
