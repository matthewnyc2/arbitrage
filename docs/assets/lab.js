/*
 * Polymarket Strategy Lab — pure browser-side backtester.
 *
 * Loads 100+ REAL resolved Polymarket categorical events from docs/data/
 * historical-events.json, runs five different strategies against them,
 * and shows you honest results — no cherry-picking.
 *
 * Each strategy is a pure function: given an event (outcomes + their
 * last-trade prices + who actually won), it returns what it would have
 * bought, how much it paid, and how much it got back. The backtest runner
 * tallies these across every event.
 *
 * The strategies live here, open and readable. You can read exactly what
 * each rule is doing.
 */

// ==============================  DATA  ====================================

const DATA_URL = "./data/historical-events.json";

// ==========================  STRATEGIES  ==================================
//
// A strategy is: strategy(event) -> { action, cost, payout, note }
//   action: "trade" if we bought anything, "skip" if we passed
//   cost:   total dollars paid (at last-trade prices)
//   payout: total dollars received after resolution
//   note:   short plain-English description of what happened
//
// Every strategy bets into the same event in its own way. Results are tallied
// across all events in the dataset.

const STRATEGIES = [
  {
    key: "basket-arb",
    name: "Basket Arbitrage",
    oneLiner: "Buy one share of every outcome — but only when the total cost is under $1.",
    rule: "If the sum of every outcome's last trade price is below $1.00, buy one share of every outcome. Otherwise skip. Exactly one outcome will win and pay $1, so you profit the gap.",
    why: "This is the textbook risk-free trade. It's the one real arbitrage on prediction markets. The question is: does it ever actually trigger in practice, on resting prices, for a retail bot that isn't co-located next to the exchange? The historical data tells the truth.",
    run(ev) {
      const prices = ev.outcomes.map(o => o.last_trade_price).filter(p => p != null && p > 0 && p < 1);
      if (prices.length !== ev.outcomes.length) {
        return { action: "skip", cost: 0, payout: 0, sum: null, note: "Missing prices for one or more outcomes — couldn't evaluate." };
      }
      const sum = prices.reduce((a, b) => a + b, 0);
      if (sum >= 1.0) {
        return { action: "skip", cost: 0, payout: 0, sum, note: `Total cost was $${sum.toFixed(3)}, above $1. No arbitrage — skipped.` };
      }
      const cost = sum;
      const payout = 1.0;  // exactly one outcome wins and pays $1
      return { action: "trade", cost, payout, sum, note: `Total cost $${sum.toFixed(3)}, below $1. Bought complete set, received $1 guaranteed.` };
    },
  },

  {
    key: "favorite",
    name: "Bet the Favorite",
    oneLiner: "On every event, buy the single outcome the market thinks is most likely.",
    rule: "For each event, buy one share of whichever outcome has the highest last-trade price. If that outcome wins, you get $1. If any other outcome wins, you get $0.",
    why: "Conventional wisdom: the market knows. If the favorite wins often enough, you make money. If the market systematically over-prices favorites, you lose. This tests whether Polymarket's favorites are priced fairly.",
    run(ev) {
      let best = null;
      for (const o of ev.outcomes) {
        if (o.last_trade_price == null) continue;
        if (!best || o.last_trade_price > best.last_trade_price) best = o;
      }
      if (!best) return { action: "skip", cost: 0, payout: 0, note: "No prices available." };
      return {
        action: "trade",
        cost: best.last_trade_price,
        payout: best.yes_final_price, // 1 if it won, 0 if it lost
        note: `Bought "${best.name}" at $${best.last_trade_price.toFixed(3)}. ${best.yes_final_price === 1 ? "It won — payout $1." : "It lost — payout $0."}`,
      };
    },
  },

  {
    key: "longshot",
    name: "Bet the Longshot",
    oneLiner: "On every event, buy the cheapest outcome. Pray it wins.",
    rule: "For each event, buy one share of whichever outcome has the lowest positive last-trade price. Small cost, huge payout if it wins — but it almost never does.",
    why: "The market prices longshots low for a reason. But is it correct? Maybe underdogs win more often than prices imply (a classic behavioral-finance bias). This strategy cleanly tests the claim.",
    run(ev) {
      let best = null;
      for (const o of ev.outcomes) {
        if (o.last_trade_price == null || o.last_trade_price <= 0) continue;
        if (!best || o.last_trade_price < best.last_trade_price) best = o;
      }
      if (!best) return { action: "skip", cost: 0, payout: 0, note: "No prices available." };
      return {
        action: "trade",
        cost: best.last_trade_price,
        payout: best.yes_final_price,
        note: `Bought "${best.name}" at $${best.last_trade_price.toFixed(3)}. ${best.yes_final_price === 1 ? "It won — payout $1." : "It lost — payout $0."}`,
      };
    },
  },

  {
    key: "equal-split",
    name: "Equal Split",
    oneLiner: "Buy one share of every outcome, always — no matter the price.",
    rule: "For each event, buy one share of every single outcome. You pay the sum of prices. You receive $1 because exactly one outcome wins.",
    why: "This is Basket Arbitrage without the safety condition — just always buy the basket. Every event is a tiny guaranteed loss equal to the &ldquo;vig&rdquo; (the amount above $1 that Polymarket's prices sum to). A baseline for what the market's average over-roundedness costs.",
    run(ev) {
      const prices = ev.outcomes.map(o => o.last_trade_price).filter(p => p != null && p > 0);
      if (prices.length !== ev.outcomes.length) {
        return { action: "skip", cost: 0, payout: 0, note: "Missing prices for one or more outcomes." };
      }
      const cost = prices.reduce((a, b) => a + b, 0);
      return { action: "trade", cost, payout: 1.0, note: `Paid $${cost.toFixed(3)} for every outcome. Guaranteed $1 payout.` };
    },
  },

  {
    key: "top-three",
    name: "Top Three",
    oneLiner: "Buy the three outcomes the market thinks are most likely. Win if any of them wins.",
    rule: "For each event with 3+ outcomes, buy one share of the three highest-priced outcomes. Pay the sum. Win $1 if any of those three wins.",
    why: "A hedged bet: you're buying most of the probability mass but skipping the tail. If the hit rate is high enough, it pays. If not, you're paying for protection you don't need.",
    run(ev) {
      const prices = ev.outcomes.filter(o => o.last_trade_price != null && o.last_trade_price > 0);
      if (prices.length < 3) return { action: "skip", cost: 0, payout: 0, note: "Event has fewer than 3 outcomes — strategy doesn't apply." };
      const top = [...prices].sort((a, b) => b.last_trade_price - a.last_trade_price).slice(0, 3);
      const cost = top.reduce((s, o) => s + o.last_trade_price, 0);
      const won = top.some(o => o.yes_final_price === 1);
      return {
        action: "trade",
        cost,
        payout: won ? 1.0 : 0.0,
        note: `Bought top 3 (total $${cost.toFixed(3)}). ${won ? "One of the three won — payout $1." : "None of the three won — payout $0."}`,
      };
    },
  },
];

// ==========================  BACKTEST RUNNER  =============================

function runBacktest(strategy, events) {
  const rows = [];
  let totalCost = 0, totalPayout = 0;
  let trades = 0, wins = 0, losses = 0, skipped = 0;

  for (const ev of events) {
    const result = strategy.run(ev);
    const pnl = (result.payout || 0) - (result.cost || 0);
    const row = { event: ev, result, pnl };
    rows.push(row);

    if (result.action === "trade") {
      trades += 1;
      totalCost += result.cost || 0;
      totalPayout += result.payout || 0;
      if (pnl > 0)      wins += 1;
      else if (pnl < 0) losses += 1;
    } else {
      skipped += 1;
    }
  }

  const pnlAbs = totalPayout - totalCost;
  const roi = totalCost > 0 ? pnlAbs / totalCost : 0;
  const winRate = trades > 0 ? wins / trades : null;

  return {
    rows,
    totalCost, totalPayout, pnlAbs, roi,
    trades, wins, losses, skipped,
    winRate,
    eventCount: events.length,
  };
}

// ==========================  STATE  =======================================

const state = {
  events: [],
  results: {},            // key -> backtest result
  activeKey: "basket-arb",
  tradeFilter: "all",
  bankroll: 1000,          // how much you'd commit per opportunity
};

// ==========================  DOM  =========================================

const $ = (s) => document.querySelector(s);
const el = {
  tabResults:     $("#tab-results"),
  tabStrategies:  $("#tab-strategies"),
  panelResults:   $("#panel-results"),
  panelStrategies:$("#panel-strategies"),
  eventCountInline: $("#event-count-inline"),
  eventCountStrat:  $("#event-count-strat"),
  activeLabel:   $("#active-strategy-label"),
  activeName:    $("#active-strategy-name"),
  activeDesc:    $("#active-strategy-desc"),
  switchBtn:     $("#switch-btn"),
  verdictCard:   $("#verdict-card"),
  verdictIcon:   $("#verdict-icon"),
  verdictLabel:  $("#verdict-label"),
  verdictDetail: $("#verdict-detail"),
  vstatPnl:      $("#vstat-pnl"),
  vstatPnlLbl:   $("#vstat-pnl-lbl"),
  vstatRoi:      $("#vstat-roi"),
  vstatTrades:   $("#vstat-trades"),
  vstatWinrate:  $("#vstat-winrate"),
  vstatAnnual:   $("#vstat-annual"),
  bankrollChoices: $("#bankroll-choices"),
  bankrollNote:  $("#bankroll-note"),
  verdictExplainer: $("#verdict-explainer"),
  cntAll:        $("#cnt-all"),
  cntTrades:     $("#cnt-trades"),
  cntWins:       $("#cnt-wins"),
  cntLosses:     $("#cnt-losses"),
  cntSkipped:    $("#cnt-skipped"),
  tradeList:     $("#trade-list"),
  strategyGrid:  $("#strategy-grid"),
  modal:         $("#strategy-modal"),
  modalContent:  $("#strategy-modal-content"),
};

// ==========================  BOOT  ========================================

boot().catch(err => {
  console.error("lab boot failed", err);
  el.verdictLabel.textContent = "Couldn't load historical data";
  el.verdictDetail.textContent = String(err.message || err);
});

async function boot() {
  const resp = await fetch(DATA_URL + "?t=" + Date.now());
  if (!resp.ok) throw new Error("historical-events.json " + resp.status);
  const payload = await resp.json();
  state.events = Array.isArray(payload?.events) ? payload.events : [];
  if (!state.events.length) throw new Error("No events found in dataset");

  // Figure out the date span covered by the 93 events so we can annualize PnL.
  const ends = state.events
    .map(e => new Date((e.closed_time || e.end_date || "").replace(" +00", "+00:00")))
    .filter(d => !isNaN(d.getTime()))
    .sort((a, b) => a - b);
  state.spanFirst = ends[0];
  state.spanLast  = ends[ends.length - 1];
  state.spanDays  = Math.max(1, (state.spanLast - state.spanFirst) / (1000 * 60 * 60 * 24));

  el.eventCountInline.textContent = `${state.events.length} events · ${formatSpanDescription(state.spanFirst, state.spanLast)}`;
  el.eventCountStrat.textContent = state.events.length;

  for (const s of STRATEGIES) {
    state.results[s.key] = runBacktest(s, state.events);
  }

  wireInteractions();
  renderStrategyGrid();
  renderActiveStrategy();
}

function formatSpanDescription(first, last, withMonths = true) {
  if (!first || !last) return "";
  const fmt = { month: "short", year: "numeric" };
  const range = `${first.toLocaleDateString(undefined, fmt)} – ${last.toLocaleDateString(undefined, fmt)}`;
  if (!withMonths) return range;
  const months = (state.spanDays / 30).toFixed(1);
  return `${range} (${months} months)`;
}
function pluralize(n, word) {
  return n === 1 ? `1 ${word}` : `${n} ${word}s`;
}

function wireInteractions() {
  // Tabs
  el.tabResults.addEventListener("click", () => switchTab("results"));
  el.tabStrategies.addEventListener("click", () => switchTab("strategies"));

  // "Change strategy" button on results page -> jumps to strategies tab
  el.switchBtn.addEventListener("click", () => switchTab("strategies"));

  // Trade filter buttons
  document.querySelectorAll(".filter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      state.tradeFilter = btn.dataset.filter;
      document.querySelectorAll(".filter-btn").forEach(b => b.classList.toggle("active", b === btn));
      renderTradeList();
    });
  });

  // CSV download
  const dl = document.getElementById("csv-download");
  if (dl) dl.addEventListener("click", (e) => { e.preventDefault(); downloadCsv(); });

  // Bankroll selector
  el.bankrollChoices.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-bankroll]");
    if (!btn) return;
    state.bankroll = parseInt(btn.dataset.bankroll, 10) || 1000;
    [...el.bankrollChoices.querySelectorAll("button")].forEach(b => b.classList.toggle("active", b === btn));
    renderActiveStrategy();
    renderStrategyGrid();
  });

  // Modal close
  el.modal.addEventListener("click", (e) => {
    if (e.target.dataset?.close !== undefined) el.modal.hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") el.modal.hidden = true;
  });
}

function switchTab(which) {
  const isResults = which === "results";
  el.tabResults.classList.toggle("active", isResults);
  el.tabStrategies.classList.toggle("active", !isResults);
  el.panelResults.classList.toggle("active", isResults);
  el.panelStrategies.classList.toggle("active", !isResults);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ==========================  RENDER: RESULTS TAB  =========================

function renderActiveStrategy() {
  const strategy = STRATEGIES.find(s => s.key === state.activeKey);
  if (!strategy) return;
  const result = state.results[strategy.key];

  el.activeLabel.textContent = "Active strategy";
  el.activeName.textContent = strategy.name;
  el.activeDesc.textContent = strategy.oneLiner;

  renderVerdict(strategy, result);
  renderTradeList();
}

function verdictClass(result) {
  const pnl = result.pnlAbs;
  if (Math.abs(pnl) < 0.005) return "flat";
  return pnl > 0 ? "win" : "loss";
}

function renderVerdict(strategy, result) {
  const cls = verdictClass(result);
  el.verdictCard.className = "verdict-card " + cls;
  el.verdictIcon.textContent = cls === "win" ? "✓" : cls === "loss" ? "✗" : "≈";

  const { roi, trades, wins, losses, eventCount } = result;
  const totalPnl = roi * state.bankroll * trades;  // bet $bankroll each trade, PnL per trade = roi*bankroll

  const months = (state.spanDays / 30).toFixed(1);
  const span   = formatSpanDescription(state.spanFirst, state.spanLast, false);
  const firedN = pluralize(trades, "time");

  if (trades === 0) {
    el.verdictLabel.textContent = "Strategy never triggered";
    el.verdictDetail.textContent = `Over ${months} months of real Polymarket events (${span}), this strategy's rule never fired even once. Pure arbitrage on resting prices almost never exists — bots eat any gap in milliseconds.`;
  } else if (cls === "win") {
    el.verdictLabel.textContent = "Made money on this dataset";
    el.verdictDetail.textContent = `Over ${months} months (${span}) this strategy fired ${firedN} across ${eventCount} events. ${wins} wins, ${losses} losses. At a $${state.bankroll.toLocaleString()} bankroll per trade, total profit was ${formatSignedDollar(totalPnl)}.`;
  } else if (cls === "loss") {
    el.verdictLabel.textContent = "Lost money on this dataset";
    el.verdictDetail.textContent = `Over ${months} months (${span}) this strategy fired ${firedN} across ${eventCount} events. ${wins} wins, ${losses} losses. At a $${state.bankroll.toLocaleString()} bankroll per trade, total loss was ${formatSignedDollar(totalPnl)}.`;
  } else {
    el.verdictLabel.textContent = "Roughly break-even";
    el.verdictDetail.textContent = `Over ${months} months (${span}) this strategy fired ${firedN} across ${eventCount} events. Total profit with a $${state.bankroll.toLocaleString()} bankroll was ${formatSignedDollar(totalPnl)} — essentially nothing.`;
  }

  el.vstatPnl.textContent = formatSignedDollar(totalPnl);
  el.vstatPnl.className = "vstat-val " + (totalPnl > 0.005 ? "pos" : totalPnl < -0.005 ? "neg" : "");
  el.vstatPnlLbl.textContent = `Total profit at $${state.bankroll.toLocaleString()} per trade`;

  el.vstatRoi.textContent = trades > 0 ? formatSignedPct(roi) : "—";
  el.vstatRoi.className = "vstat-val " + (roi > 0.0001 ? "pos" : roi < -0.0001 ? "neg" : "");
  el.vstatTrades.textContent = `${trades} of ${eventCount}`;
  el.vstatTrades.className = "vstat-val";
  el.vstatWinrate.textContent = trades > 0 ? `${(result.winRate * 100).toFixed(1)}%` : "—";
  el.vstatWinrate.className = "vstat-val";

  // Annualized profit: scale the total by (365 / span)
  const annualPnl = totalPnl * (365 / state.spanDays);
  el.vstatAnnual.textContent = trades > 0 ? formatSignedDollar(annualPnl) : "—";
  el.vstatAnnual.className = "vstat-val " + (annualPnl > 0.005 ? "pos" : annualPnl < -0.005 ? "neg" : "");

  el.verdictExplainer.innerHTML = strategy.why;
}

function renderTradeList() {
  const strategy = STRATEGIES.find(s => s.key === state.activeKey);
  const result = state.results[strategy.key];
  const all = result.rows;

  const filters = {
    all:      (r) => true,
    trades:   (r) => r.result.action === "trade",
    wins:     (r) => r.result.action === "trade" && r.pnl > 0,
    losses:   (r) => r.result.action === "trade" && r.pnl < 0,
    skipped:  (r) => r.result.action === "skip",
  };
  const filtered = all.filter(filters[state.tradeFilter]);

  // counts
  el.cntAll.textContent     = all.length;
  el.cntTrades.textContent  = all.filter(filters.trades).length;
  el.cntWins.textContent    = all.filter(filters.wins).length;
  el.cntLosses.textContent  = all.filter(filters.losses).length;
  el.cntSkipped.textContent = all.filter(filters.skipped).length;

  // sort: trades first (by |pnl| desc), then skipped
  filtered.sort((a, b) => {
    const aAct = a.result.action === "trade" ? 0 : 1;
    const bAct = b.result.action === "trade" ? 0 : 1;
    if (aAct !== bAct) return aAct - bAct;
    return Math.abs(b.pnl) - Math.abs(a.pnl);
  });

  el.tradeList.innerHTML = "";
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "trade-show-more";
    empty.style.cursor = "default";
    empty.textContent = "No trades match this filter.";
    el.tradeList.appendChild(empty);
    return;
  }
  // Show every row. If you claim N trades, you show N trades.
  for (const r of filtered) {
    el.tradeList.appendChild(renderTradeRow(r));
  }
  const footer = document.createElement("div");
  footer.className = "trade-count-footer";
  footer.innerHTML = `Showing all <strong>${filtered.length}</strong> ${filtered.length === 1 ? "row" : "rows"} · <a href="#" id="csv-download">download as CSV</a>`;
  el.tradeList.appendChild(footer);
  const dl = document.getElementById("csv-download");
  if (dl) dl.addEventListener("click", (e) => { e.preventDefault(); downloadCsv(); });
}

function downloadCsv() {
  const strategy = STRATEGIES.find(s => s.key === state.activeKey);
  const result = state.results[strategy.key];
  const rows = [["event_title", "neg_risk", "num_outcomes", "action", "cost", "payout", "pnl", "note"]];
  for (const r of result.rows) {
    rows.push([
      r.event.title,
      String(r.event.neg_risk),
      String(r.event.num_outcomes),
      r.result.action,
      (r.result.cost || 0).toFixed(4),
      (r.result.payout || 0).toFixed(4),
      r.pnl.toFixed(4),
      (r.result.note || "").replace(/[\r\n]+/g, " "),
    ]);
  }
  const csv = rows.map(row => row.map(v => {
    const s = String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `polymarket-backtest-${strategy.key}.csv`;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(url); document.body.removeChild(a); }, 0);
}

function renderTradeRow(r) {
  const row = document.createElement("div");
  const didTrade = r.result.action === "trade";
  const cls = didTrade
    ? (r.pnl > 0 ? "win" : r.pnl < 0 ? "loss" : "skip")
    : "skip";
  row.className = "trade-row " + cls;

  // Scale by bankroll: if backtest cost was $0.40 for one share, and bankroll is
  // $1000, the trader would buy $1000/$0.40 = 2500 units — scaled pnl = roi * bankroll.
  const unitRoi = r.result.cost > 0 ? (r.pnl / r.result.cost) : 0;
  const scaledCost = didTrade ? state.bankroll : 0;
  const scaledPayout = didTrade ? state.bankroll * (1 + unitRoi) : 0;
  const scaledPnl = scaledPayout - scaledCost;

  const meta = didTrade
    ? `paid $${scaledCost.toLocaleString(undefined, {maximumFractionDigits:2})} → got back $${scaledPayout.toLocaleString(undefined, {maximumFractionDigits:2})}`
    : (r.result.note || "Strategy did not trade this event.");

  const resultCell = didTrade
    ? (scaledPnl > 0.005
        ? `<span class="trade-result pos">+$${scaledPnl.toLocaleString(undefined, {maximumFractionDigits:2})}</span>`
        : scaledPnl < -0.005
          ? `<span class="trade-result neg">-$${Math.abs(scaledPnl).toLocaleString(undefined, {maximumFractionDigits:2})}</span>`
          : `<span class="trade-result neutral">$0.00</span>`)
    : `<span class="trade-result neutral">skipped</span>`;

  row.innerHTML = `
    <div class="trade-event">
      <div class="trade-title">${escapeHtml(r.event.title)}</div>
      <div class="trade-meta">${escapeHtml(meta)}</div>
    </div>
    <div class="trade-action">${escapeHtml(didTrade ? r.result.note : "")}</div>
    ${resultCell}
  `;
  return row;
}

// ==========================  RENDER: STRATEGIES TAB  ======================

function renderStrategyGrid() {
  el.strategyGrid.innerHTML = "";
  for (const s of STRATEGIES) {
    const r = state.results[s.key];
    const cls = verdictClass(r);
    const card = document.createElement("div");
    card.className = "strat-card" + (s.key === state.activeKey ? " active" : "");
    const metric = r.trades > 0 ? formatSignedPct(r.roi) : "never fired";
    const totalScaledPnl = r.roi * state.bankroll * r.trades;
    const metricSub = r.trades > 0
      ? `${formatSignedDollar(totalScaledPnl)} total at $${state.bankroll.toLocaleString()}/trade · ${r.trades} trades`
      : `skipped all ${r.eventCount} events`;
    const verdictLabel = r.trades === 0 ? "INACTIVE"
                       : cls === "win"  ? "PROFITABLE"
                       : cls === "loss" ? "LOSES MONEY"
                       : "BREAK-EVEN";

    card.innerHTML = `
      <div class="strat-card-head">
        <div class="strat-card-name">${escapeHtml(s.name)}</div>
        <div class="strat-card-badge ${cls}">${verdictLabel}</div>
      </div>
      <p class="strat-card-desc">${escapeHtml(s.oneLiner)}</p>
      <div class="strat-card-metric ${cls}">${metric}</div>
      <div class="strat-card-metric-sub">${escapeHtml(metricSub)}</div>
      <div class="strat-card-stats">
        <div class="strat-card-stat">trades: <strong>${r.trades}</strong></div>
        <div class="strat-card-stat">wins: <strong>${r.wins}</strong></div>
        <div class="strat-card-stat">losses: <strong>${r.losses}</strong></div>
      </div>
      <div class="strat-card-learn">Learn more & use this strategy →</div>
    `;
    card.addEventListener("click", () => openStrategyModal(s));
    el.strategyGrid.appendChild(card);
  }
}

function openStrategyModal(s) {
  const r = state.results[s.key];
  const cls = verdictClass(r);
  const verdictLabel = r.trades === 0 ? "STRATEGY NEVER FIRED"
                     : cls === "win"  ? "PROFITABLE ON THIS DATASET"
                     : cls === "loss" ? "LOSES MONEY ON THIS DATASET"
                     : "ROUGHLY BREAK-EVEN";

  el.modalContent.innerHTML = `
    <div class="strategy-detail">
      <h2>${escapeHtml(s.name)}</h2>
      <div class="detail-verdict ${cls}">${verdictLabel}</div>

      <div class="detail-rule"><strong>The rule:</strong> ${s.rule}</div>

      <div class="detail-section">
        <h3>Why this strategy?</h3>
        <p>${s.why}</p>
      </div>

      <div class="detail-section">
        <h3>Results on ${r.eventCount} real resolved events</h3>
        <div class="detail-stats">
          <div class="dstat">
            <div class="dstat-val ${r.pnlAbs > 0 ? 'pos' : r.pnlAbs < 0 ? 'neg' : ''}">${formatSignedDollar(r.pnlAbs)}</div>
            <div class="dstat-lbl">total profit</div>
          </div>
          <div class="dstat">
            <div class="dstat-val ${r.roi > 0 ? 'pos' : r.roi < 0 ? 'neg' : ''}">${r.trades > 0 ? formatSignedPct(r.roi) : '—'}</div>
            <div class="dstat-lbl">ROI per dollar</div>
          </div>
          <div class="dstat">
            <div class="dstat-val">${r.trades}</div>
            <div class="dstat-lbl">trades taken</div>
          </div>
          <div class="dstat">
            <div class="dstat-val">${r.trades > 0 ? (r.winRate * 100).toFixed(1) + '%' : '—'}</div>
            <div class="dstat-lbl">win rate</div>
          </div>
        </div>
      </div>

      <div class="cta-row">
        <button type="button" class="cta-primary" id="use-strategy">Run this strategy on Results tab</button>
        <button type="button" class="cta-secondary" data-close>Close</button>
      </div>
    </div>
  `;
  el.modal.hidden = false;
  document.getElementById("use-strategy").addEventListener("click", () => {
    state.activeKey = s.key;
    state.tradeFilter = "all";
    document.querySelectorAll(".filter-btn").forEach(b => b.classList.toggle("active", b.dataset.filter === "all"));
    renderActiveStrategy();
    renderStrategyGrid();
    el.modal.hidden = true;
    switchTab("results");
  });
}

// ==========================  UTILS  =======================================

function formatSignedDollar(x) {
  const sign = x >= 0 ? "+" : "−";
  return sign + "$" + Math.abs(x).toFixed(2);
}
function formatSignedPct(x) {
  const sign = x >= 0 ? "+" : "−";
  return sign + Math.abs(x * 100).toFixed(2) + "%";
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
