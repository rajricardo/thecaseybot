"use strict";
/*
 * Casey Bridge frontend. No build step, no framework — plain fetch polling
 * against web/server.py's /api/* routes every 2.5s (matches the worker
 * thread's own snapshot cadence in trade_executor._snapshot_state).
 *
 * The Signals search input and the whole Settings screen are deliberately
 * NOT rebuilt on every poll tick (see renderScreen()) — this app rebuilds
 * screens via innerHTML, which would otherwise steal focus/cursor position
 * out from under anyone actively typing every single poll.
 */

const POLL_MS = 2500;

let state = null;
// settingsSaved is a frozen clone taken alongside settingsDraft when the
// Settings screen (re)initializes — never mutated by user input, only used
// as the "nothing to save" baseline isSettingsDirty() compares against.
const ui = { screen: "dash", filter: "ALL", query: "", settingsDraft: null, settingsSaved: null };

const COLORS = {
  ENTRY: { fg: "#003c33", bg: "#edfce9", border: "#edfce9" },
  EXIT: { fg: "#b30000", bg: "#ffffff", border: "#d9d9dd" },
  TRIM: { fg: "#c0431f", bg: "#ffffff", border: "#ffad9b" },
  ADD: { fg: "#1863dc", bg: "#f1f5ff", border: "#f1f5ff" },
  NOISE: { fg: "#75758a", bg: "#ffffff", border: "#d9d9dd" },
};

// The Model dropdown's only choices — llm.model is sent straight to
// Anthropic's API (see llm_classifier.py's classify()), so this list needs
// to be kept current with whatever models the bot should be allowed to run.
const KNOWN_MODELS = [
  { id: "claude-haiku-4-5-20251001", label: "Haiku 4.5" },
  { id: "claude-sonnet-5", label: "Sonnet 5" },
  { id: "claude-opus-5", label: "Opus 5" },
  { id: "claude-fable-5", label: "Fable 5" },
];

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  const sign = v > 0 ? "+$" : v < 0 ? "−$" : "$";
  return sign + Math.abs(v).toFixed(2);
}

function moneyColor(v) {
  if (v === null || v === undefined) return "#212121";
  return v > 0 ? "#003c33" : v < 0 ? "#b30000" : "#212121";
}

function fmtTime(ts) {
  if (!ts) return "--:--";
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  return `${Math.round(s / 60)}m ago`;
}

async function postJSON(url, body) {
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    await fetchState();
    return r.ok;
  } catch (e) {
    console.error("POST " + url + " failed", e);
    return false;
  }
}

// ── shell (built once; polling only ever touches specific containers) ──

function initShell() {
  document.getElementById("app").innerHTML = `
    <div class="bar" id="bar"></div>
    <div class="layout">
      <div class="main">
        <header class="header">
          <div class="brand"><div class="mark"></div><div class="name display">Casey Bridge</div></div>
          <div class="controls">
            <input class="search-input" id="search-input" placeholder="Search messages">
            <button class="run-btn" id="run-btn" data-action="toggle-run">
              <span class="dot" id="run-dot"></span><span id="run-label"></span>
            </button>
          </div>
          <nav class="subtabs" id="subtabs"></nav>
        </header>
        <div class="screen-wrap" id="screen"></div>
      </div>
      <aside class="aside" id="aside"></aside>
    </div>
  `;

  document.getElementById("search-input").addEventListener("input", (e) => {
    ui.query = e.target.value;
    ui.screen = "feed";
    renderSubtabs();
    renderScreen();
  });

  document.getElementById("app").addEventListener("click", handleDelegatedClick);
}

function handleDelegatedClick(e) {
  const el = e.target.closest("[data-action]");
  if (!el) return;
  const action = el.dataset.action;

  if (action === "toggle-run") {
    postJSON(state.bar.paused ? "/api/bot/resume" : "/api/bot/pause");
  } else if (action === "nav") {
    ui.screen = el.dataset.screen;
    render();
  } else if (action === "filter") {
    ui.filter = el.dataset.filter;
    renderScreen();
  } else if (action === "set-mode") {
    postJSON("/api/mode", { mode: el.dataset.mode });
  } else if (action === "pos-trim" || action === "pos-add" || action === "pos-close") {
    const kind = action.slice(4);
    el.disabled = true;
    postJSON(`/api/positions/${encodeURIComponent(el.dataset.ticker)}/${kind}`);
  } else if (action === "settings-pill") {
    setDraft(el.dataset.path, el.dataset.value);
    rerenderSettingsForm();
  } else if (action === "remove-ticker") {
    el.disabled = true;
    postJSON(`/api/tickers/${encodeURIComponent(el.dataset.ticker)}/remove`);
  } else if (action === "add-ticker") {
    addTicker();
  } else if (action === "settings-loss-toggle") {
    const r = ui.settingsDraft.risk;
    r.daily_loss_limit = r.daily_loss_limit == null ? 500 : null;
    rerenderSettingsForm();
  } else if (action === "settings-losing-trades-toggle") {
    const r = ui.settingsDraft.risk;
    r.max_daily_losing_trades = r.max_daily_losing_trades == null ? 3 : null;
    rerenderSettingsForm();
  } else if (action === "settings-toggle-auto-stop") {
    // toggled as a real JS boolean, not via settings-pill/data-value —
    // data attributes are always strings, and sending "false" (a truthy
    // string) as a YAML value would round-trip wrong.
    ui.settingsDraft.risk.auto_submit_stop_loss = !ui.settingsDraft.risk.auto_submit_stop_loss;
    rerenderSettingsForm();
  } else if (action === "settings-toggle-retry-reconnect") {
    const rc = ui.settingsDraft.reconnect;
    rc.retry_on_reconnect = !rc.retry_on_reconnect;
    // the timeout is always enforced once retry is on — never left blank
    // for a freshly-enabled toggle just because it happened to be null
    // (e.g. an older config.yaml written before the timeout was mandatory).
    if (rc.retry_on_reconnect && rc.retry_timeout_mins == null) rc.retry_timeout_mins = 5;
    rerenderSettingsForm();
  } else if (action === "settings-save") {
    saveSettings();
  }
}

// Rebuilds the settings form from the *existing* draft (never resets it
// from the polled state) — used after a pill/toggle click, where a full
// innerHTML rebuild is harmless since none of those are text inputs mid-edit.
function rerenderSettingsForm() {
  document.getElementById("screen").innerHTML = renderSet();
  wireSettingsInputs();
  renderTickerChips();
}

function setDraft(path, value) {
  const parts = path.split(".");
  let obj = ui.settingsDraft;
  for (let i = 0; i < parts.length - 1; i++) obj = obj[parts[i]];
  obj[parts[parts.length - 1]] = value;
}

// ── polling ──

async function fetchState() {
  try {
    const r = await fetch("/api/state");
    state = await r.json();
  } catch (e) {
    console.error("fetchState failed", e);
    return;
  }
  render();
}

function render() {
  if (!state) return;
  renderBar();
  renderRunButton();
  renderSubtabs();
  renderScreen();
  renderTickerChips(); // independent of renderScreen's settings-form freeze — see its own comment
  renderAside();
}

// ── bar / header ──

function renderBar() {
  const { paused, mode, status_text } = state.bar;
  const dotColor = paused ? "#93939f" : mode === "live" ? "#ff7759" : "#7de0a0";
  const anim = paused ? "none" : "blink 2.4s infinite";
  document.getElementById("bar").innerHTML = `
    <span class="dot" style="background:${dotColor};animation:${anim}"></span>
    <span>${escapeHtml(status_text)}</span>
    <span class="now mono">${new Date().toTimeString().slice(0, 5)}</span>
  `;
}

function renderRunButton() {
  const paused = state.bar.paused;
  document.getElementById("run-dot").style.background = paused ? "#93939f" : "#7de0a0";
  document.getElementById("run-label").textContent = paused ? "Start bot" : "Stop bot";
}

const SUBTABS = [
  ["dash", "Overview"], ["feed", "Signals"], ["pos", "Positions"],
  ["set", "Settings"], ["hist", "History"],
];

function renderSubtabs() {
  const counts = {
    dash: "", feed: state.feed.length, pos: state.positions.length,
    set: "", hist: state.history.length,
  };
  document.getElementById("subtabs").innerHTML = SUBTABS.map(([id, label]) => `
    <button class="subtab ${ui.screen === id ? "active" : ""}" data-action="nav" data-screen="${id}">
      ${label}<span class="count">${counts[id]}</span>
    </button>
  `).join("");
}

// ── screen dispatch ──

const HEADS = {
  dash: ["Session overview", "Everything the terminal log showed, on one screen: what Casey said, what the bot decided, and what reached Interactive Brokers."],
  feed: ["Signal feed", "Every message from the watched channel, with the verdict, the stage that decided it, and the resulting order."],
  pos: ["Positions", "Live from Interactive Brokers, refreshed by the worker thread every few seconds. The bot reconciles against this before every order."],
  set: ["Settings", "Risk changes apply on the next signal; Discord/IBKR/LLM changes need a bot restart."],
  hist: ["History", "Closed round trips — persisted the moment a position goes flat at IBKR."],
};

function renderScreen() {
  const el = document.getElementById("screen");

  if (ui.screen === "set") {
    if (!ui.settingsDraft) {
      ui.settingsDraft = JSON.parse(JSON.stringify(state.settings));
      ui.settingsSaved = JSON.parse(JSON.stringify(state.settings));
      el.innerHTML = renderSet();
      wireSettingsInputs();
    }
    // deliberately not rebuilt again while the user stays on this screen —
    // see the module docstring.
    return;
  }
  ui.settingsDraft = null;
  ui.settingsSaved = null;

  const [heading, sub] = HEADS[ui.screen];
  const body = ui.screen === "dash" ? renderDash()
    : ui.screen === "feed" ? renderFeed()
    : ui.screen === "pos" ? renderPos()
    : renderHist();

  el.innerHTML = `
    <div class="screen-heading">
      <div>
        <div class="kicker">Session</div>
        <h2 class="display">${heading}</h2>
        <p>${sub}</p>
      </div>
      ${ui.screen === "dash" ? renderModeToggle() : ""}
    </div>
    ${body}
  `;
}

function renderModeToggle() {
  const live = state.bar.mode === "live";
  return `
    <div class="mode-toggle">
      <button class="${!live ? "active" : ""}" data-action="set-mode" data-mode="dry">Dry run</button>
      <button class="${live ? "active" : ""}" data-action="set-mode" data-mode="live">Live</button>
    </div>
  `;
}

// ── Overview ──

function renderDash() {
  const k = state.kpis;
  const pf = k.profit_factor;
  const winPct = k.win_pct;
  return `
    <div style="display:flex;flex-direction:column;gap:56px">
      <div class="kpi-grid">
        <div class="kpi-cell">
          <div class="kpi-label">Net P&amp;L</div>
          <div class="kpi-value mono" style="color:${moneyColor(k.net_pnl)}">${fmtMoney(k.net_pnl)}</div>
          <div class="kpi-pill">${k.trade_count} trades</div>
        </div>
        <div class="kpi-cell">
          <div class="kpi-label">Profit factor</div>
          <div class="kpi-value mono">${pf === null ? "—" : pf.toFixed(2)}</div>
        </div>
        <div class="kpi-cell">
          <div class="kpi-label">Trade win %</div>
          <div class="kpi-value mono">${winPct === null ? "—" : winPct.toFixed(1) + "%"}</div>
          <div style="display:flex;gap:6px">
            <span class="tag" style="background:#edfce9;color:#003c33">${k.n_wins}</span>
            <span class="tag" style="background:#f1f5ff;color:#1863dc">${k.n_flat}</span>
            <span class="tag" style="border:1px solid #d9d9dd;color:#b30000">${k.n_losses}</span>
          </div>
        </div>
        <div class="kpi-cell">
          <div class="kpi-label">Avg win / loss trade</div>
          <div class="kpi-value mono">${k.avg_win === null && k.avg_loss === null ? "—" : ""}</div>
          <div style="display:flex;gap:10px;font-family:'Space Mono',monospace;font-size:13px">
            <span style="color:#003c33">${fmtMoney(k.avg_win)}</span>
            <span style="margin-left:auto;color:#b30000">${fmtMoney(k.avg_loss)}</span>
          </div>
        </div>
      </div>

      <div class="pipeline">
        <div class="kicker">Pipeline</div>
        <div class="item">
          <span class="dot" style="background:${state.health.discord.connected ? "#003c33" : "#93939f"};animation:${state.health.discord.connected ? "blink 2.4s infinite" : "none"}"></span>
          <span>Discord</span>
          <span class="detail">${state.health.discord.connected ? "channel " + escapeHtml(state.health.discord.channel) : "not connected"}</span>
        </div>
        <div class="item">
          <span class="dot" style="background:#003c33"></span>
          <span>Claude</span>
          <span class="detail">${state.health.claude.call_count} calls</span>
        </div>
        <div class="item">
          <span class="dot" style="background:${state.health.ibkr.connected ? "#003c33" : "#93939f"}"></span>
          <span>IBKR</span>
          <span class="detail">${state.health.ibkr.connected ? "connected" : "not connected"}</span>
        </div>
      </div>

      <div>
        <div class="section-title">
          <h4 class="display">Open positions</h4>
          <span class="note">${state.positions.length} open · ${fmtAgo(state.health.ibkr.last_snapshot_ts)}</span>
        </div>
        <div>${state.positions.length ? state.positions.map(renderPosRow).join("") : '<div class="empty">Nothing open.</div>'}</div>
      </div>
    </div>
  `;
}

function renderPosRow(p) {
  return `
    <div class="pos-row">
      <span class="instrument mono">${escapeHtml(p.contract_label)}</span>
      <div class="stats">
        <div><div class="stat-label">Qty</div><div class="stat-value mono">${p.qty}</div></div>
        <div><div class="stat-label">Avg</div><div class="stat-value mono">${p.avg != null ? p.avg.toFixed(2) : "—"}</div></div>
        <div><div class="stat-label">Last</div><div class="stat-value mono">${p.last != null ? p.last.toFixed(2) : "—"}</div></div>
      </div>
      <div class="stop">${escapeHtml(p.stop_desc || "")}</div>
      <span class="pnl mono" style="color:${moneyColor(p.unrealized_pnl)}">${fmtMoney(p.unrealized_pnl)}</span>
      <div class="actions">
        <button class="btn-pill btn-add" data-action="pos-add" data-ticker="${escapeHtml(p.ticker)}">ADD</button>
        <button class="btn-pill btn-trim" data-action="pos-trim" data-ticker="${escapeHtml(p.ticker)}">TRIM</button>
        <button class="btn-pill btn-close" data-action="pos-close" data-ticker="${escapeHtml(p.ticker)}">CLOSE</button>
      </div>
    </div>
  `;
}

// ── Signals feed ──

const FILTERS = ["ALL", "ENTRY", "TRIM", "ADD", "EXIT", "NOISE"];

function renderFeed() {
  const q = ui.query.trim().toLowerCase();
  const rows = state.feed.filter((r) =>
    (ui.filter === "ALL" || r.type === ui.filter) &&
    (!q || `${r.raw_text} ${r.ticker || ""} ${r.outcome_text || ""}`.toLowerCase().includes(q))
  );

  return `
    <div style="display:flex;flex-direction:column;gap:24px">
      <div class="filter-row">
        ${FILTERS.map((f) => `
          <button class="filter-pill ${ui.filter === f ? "active" : ""}" data-action="filter" data-filter="${f}">
            ${f === "ALL" ? "All" : f.charAt(0) + f.slice(1).toLowerCase()}
          </button>
        `).join("")}
        <span class="filter-count">${rows.length} shown of ${state.feed.length}</span>
      </div>
      <div>
        <div class="feed-head">
          <div style="width:72px">Time</div>
          <div style="width:112px">Verdict</div>
          <div style="flex:1 1 260px">Raw message</div>
          <div style="flex:1 1 200px">Order outcome</div>
        </div>
        ${rows.length ? rows.map(renderFeedRow).join("") : '<div class="empty">No signals yet.</div>'}
      </div>
    </div>
  `;
}

function renderFeedRow(r) {
  const c = COLORS[r.type] || COLORS.NOISE;
  const dim = r.type === "NOISE" ? "opacity:.6" : "";
  const outcome = r.blocked_reason
    ? `Blocked: ${escapeHtml(r.blocked_reason)}`
    : escapeHtml(r.outcome_text || (r.type === "NOISE" ? "No order." : "Pending…"));
  const outFg = r.outcome_failed || r.blocked_reason ? "#b30000" : (r.type === "NOISE" ? "#93939f" : "#212121");
  return `
    <div class="feed-row" style="${dim}">
      <div class="time mono">${fmtTime(r.ts)}</div>
      <div class="verdict">
        <span class="tag" style="background:${c.bg};color:${c.fg};border-color:${c.border}">${r.type}</span>
        <span class="stage">${r.stage === "claude" ? "Claude decided" : r.stage === "manual" ? "Manual UI" : "Regex matched"}</span>
      </div>
      <div class="raw">
        <div class="text">“${escapeHtml(r.raw_text)}”</div>
        <div class="reason">${escapeHtml(r.reason || "")}</div>
      </div>
      <div class="outcome">
        <div class="instrument mono">${escapeHtml(r.outcome_instrument || r.ticker || "—")}</div>
        <div class="text" style="color:${outFg}">${outcome}</div>
      </div>
    </div>
  `;
}

// ── Positions table ──

function renderPos() {
  const total = state.positions.reduce((s, p) => s + (p.unrealized_pnl || 0), 0);
  const stops = state.positions.filter((p) => p.stop_desc && !p.stop_desc.startsWith("No")).length;
  return `
    <div style="display:flex;flex-direction:column;gap:40px">
      <div class="kpi-grid" style="border-radius:8px">
        <div class="kpi-cell" style="border:none;background:#eeece7;border-radius:8px;margin-right:16px">
          <div class="kpi-label">Unrealized</div>
          <div class="kpi-value mono" style="color:${moneyColor(total)}">${fmtMoney(total)}</div>
          <div class="field-note">Across ${state.positions.length} contract(s)</div>
        </div>
        <div class="kpi-cell" style="border:none;background:#eeece7;border-radius:8px;margin-right:16px">
          <div class="kpi-label">Realized today</div>
          <div class="kpi-value mono" style="color:${moneyColor(state.kpis.realized_today)}">${fmtMoney(state.kpis.realized_today)}</div>
        </div>
        <div class="kpi-cell" style="border:none;background:#eeece7;border-radius:8px">
          <div class="kpi-label">Protective stops</div>
          <div class="kpi-value mono">${stops} working</div>
        </div>
      </div>
      <div>
        <div class="section-title" style="border-bottom:1px solid #212121;padding-bottom:14px">
          <h4 class="display" style="font-size:24px">Held at Interactive Brokers</h4>
          <span class="note" style="margin-left:auto">The bot never trades on the message alone</span>
        </div>
        <table>
          <thead><tr>
            <th>Contract</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">Last</th>
            <th class="num">Unrealized</th><th>Protective stop</th>
          </tr></thead>
          <tbody>
            ${state.positions.map((p) => `
              <tr>
                <td class="mono">${escapeHtml(p.contract_label)}</td>
                <td class="num mono">${p.qty}</td>
                <td class="num mono">${p.avg != null ? p.avg.toFixed(2) : "—"}</td>
                <td class="num mono">${p.last != null ? p.last.toFixed(2) : "—"}</td>
                <td class="num mono" style="color:${moneyColor(p.unrealized_pnl)}">${fmtMoney(p.unrealized_pnl)}</td>
                <td>${escapeHtml(p.stop_desc || "")}</td>
              </tr>
            `).join("") || '<tr><td colspan="6" class="empty">Nothing open.</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

// ── History ──

function renderHist() {
  const realized = state.history.reduce((s, h) => s + (h.realized_pnl || 0), 0);
  return `
    <div>
      <div class="section-title" style="border-bottom:1px solid #212121;padding-bottom:14px">
        <h4 class="display" style="font-size:24px">Closed round trips</h4>
        <span class="note" style="margin-left:auto">realized <span class="mono" style="color:#003c33">${fmtMoney(realized)}</span></span>
      </div>
      <table>
        <thead><tr>
          <th>Date</th><th>Contract</th><th class="num">Realized</th><th>Mode</th>
        </tr></thead>
        <tbody>
          ${state.history.map((h) => `
            <tr>
              <td class="mono" style="color:#75758a">${new Date(h.closed_ts * 1000).toLocaleDateString(undefined, { month: "short", day: "2-digit" })}</td>
              <td class="mono">${escapeHtml(h.contract_label)}</td>
              <td class="num mono" style="color:${moneyColor(h.realized_pnl)}">${fmtMoney(h.realized_pnl)}</td>
              <td><span class="tag" style="border:1px solid #212121;color:#212121">${h.mode}</span></td>
            </tr>
          `).join("") || '<tr><td colspan="4" class="empty">No closed trades yet.</td></tr>'}
        </tbody>
      </table>
    </div>
  `;
}

// ── Settings ──

function pillRow(path, options, currentVal, labelFn) {
  labelFn = labelFn || ((o) => o);
  return `<div class="pill-row">${options.map((o) => `
    <button class="pill-toggle ${String(currentVal) === String(o) ? "active" : ""}"
      data-action="settings-pill" data-path="${path}" data-value="${o}">${labelFn(o)}</button>
  `).join("")}</div>`;
}

// Ticker chips are rebuilt from live polled state on every tick (see
// render()'s call to renderTickerChips below) — NOT from ui.settingsDraft
// like the rest of this screen. config.yaml is the source of truth here:
// add/remove hit the backend immediately (no batched Save step) so the
// file is never out of sync with what's displayed, and chips are plain
// buttons with nothing to lose focus on, so a poll-driven rebuild is safe.
function renderTickerChips() {
  const el = document.getElementById("ticker-chips");
  if (!el || !state) return;
  const tickers = state.settings.risk.allowed_tickers;
  el.innerHTML = tickers.map((t) => `
    <button class="pill-toggle active" data-action="remove-ticker" data-ticker="${t}"
      ${tickers.length <= 1 ? "disabled title=\"Can't remove the last allowed ticker\"" : `title="Remove ${t}"`}>
      ${t} <span aria-hidden="true">✕</span>
    </button>
  `).join("");
}

function renderSet() {
  const d = ui.settingsDraft;
  const r = d.risk;
  const rc = d.reconnect;

  return `
    <div class="screen-heading">
      <div>
        <div class="kicker">Session</div>
        <h2 class="display">Settings</h2>
        <p>Risk changes apply on the next signal; Discord/IBKR/LLM changes need a bot restart.</p>
      </div>
    </div>
    <div class="settings-grid">
      <section class="card">
        <h4>Risk &amp; sizing</h4>
        <div class="field-kicker">Allowed tickers</div>
        <div class="pill-row" id="ticker-chips" style="margin-bottom:12px"></div>
        <div class="add-ticker-row" style="margin-bottom:28px">
          <input id="new-ticker-input" class="text-input mono" placeholder="Add ticker, e.g. META" style="max-width:160px" maxlength="10">
          <button class="btn-pill btn-add" id="add-ticker-btn" data-action="add-ticker">+ Add</button>
          <span id="ticker-add-status" class="field-note" style="margin:0"></span>
        </div>
        <div style="display:flex;flex-direction:column;gap:26px">
          <div>
            <div class="field-kicker">Contract sizing</div>
            ${pillRow("risk.sizing_mode", ["fixed", "dynamic"], r.sizing_mode || "fixed", (o) => o.toUpperCase())}
          </div>
          ${(r.sizing_mode || "fixed") === "dynamic" ? `
          <div>
            <div class="slider-row"><span>Capital per trade</span></div>
            <div style="display:flex;align-items:center;gap:12px;margin-top:14px">
              <span class="mono" style="font-size:18px">$</span>
              <input type="number" step="50" min="0" id="set-capital-per-trade" value="${r.capital_per_trade ?? ""}" class="text-input" style="width:140px">
            </div>
          </div>
          ` : `
          <div>
            <div class="slider-row"><span>Contracts per entry</span><span class="val mono" id="set-size-val">${r.max_contracts_per_trade}</span></div>
            <input type="range" min="1" max="50" step="1" value="${r.max_contracts_per_trade}" id="set-size">
          </div>
          `}
          <div>
            <div class="slider-row"><span>Trim size</span><span style="font-size:14px;color:#616161">% of held position sold on TRIM</span><span class="val mono" id="set-trim-val">${r.trim_pct}%</span></div>
            <input type="range" min="10" max="90" step="5" value="${r.trim_pct}" id="set-trim">
          </div>
          <div style="display:flex;align-items:baseline;gap:12px">
            <span style="font-size:16px">Auto-protect after TRIM</span>
            <button class="btn-pill" style="margin-left:auto;${r.auto_submit_stop_loss ? "background:#17171c;color:#fff" : "border:1px solid #d9d9dd;color:#17171c"}"
              data-action="settings-toggle-auto-stop">${r.auto_submit_stop_loss ? "Enabled" : "Disabled"}</button>
          </div>
          <p class="field-note" style="margin-top:-14px">When on, every TRIM places a protective stop on the remaining runners at the average entry price. When off, TRIM sells but leaves the rest unprotected.</p>
          <div>
            <div class="slider-row"><span>Add size</span><span style="font-size:14px;color:#616161">% of held position bought on ADD</span><span class="val mono" id="set-add-val">${r.add_pct}%</span></div>
            <input type="range" min="10" max="200" step="10" value="${r.add_pct}" id="set-add">
          </div>
          <div class="hr">
            <div class="field-kicker">Strike offset</div>
            ${pillRow("risk.strike_offset", ["3ITM", "2ITM", "1ITM", "1OTM", "2OTM", "3OTM"], r.strike_offset)}
            <p class="field-note">Rank in or out of the money. There is no ATM — 1OTM is the closest-to-money strike.</p>
          </div>
          <div>
            <div class="field-kicker">Expiry selection</div>
            ${pillRow("risk.expiry_selection", ["nearest", "weeklies"], r.expiry_selection, (o) => o.toUpperCase())}
          </div>
          <div>
            <div class="field-kicker">Price type</div>
            ${pillRow("risk.price_type", ["AUTO", "MARKET", "MIDPOINT", "BID", "ASK", "LAST"], r.price_type)}
            <p class="field-note">AUTO submits MIDPOINT before 9:45am ET and switches to MARKET from 9:45am ET onward, since IBKR doesn't accept MARKET orders on options right at the open.</p>
          </div>
          <div class="hr">
            <div style="display:flex;align-items:baseline;gap:12px">
              <span style="font-size:16px">Max daily losing trades</span>
              <button class="btn-pill" style="margin-left:auto;${r.max_daily_losing_trades != null ? "background:#17171c;color:#fff" : "border:1px solid #d9d9dd;color:#17171c"}"
                data-action="settings-losing-trades-toggle">${r.max_daily_losing_trades != null ? "Enforced" : "Disabled"}</button>
            </div>
            <div style="display:flex;align-items:center;gap:12px;margin-top:14px">
              <input type="number" step="1" min="0" id="set-max-losing-trades" value="${r.max_daily_losing_trades ?? ""}" ${r.max_daily_losing_trades == null ? "disabled" : ""}
                class="text-input" style="width:140px">
            </div>
            <p class="field-note">No new ENTRY/ADD once this many trades close as losers today. TRIM/EXIT are never blocked.</p>
          </div>
          <div>
            <div style="display:flex;align-items:baseline;gap:12px">
              <span style="font-size:16px">Daily loss limit</span>
              <button class="btn-pill" style="margin-left:auto;${r.daily_loss_limit != null ? "background:#17171c;color:#fff" : "border:1px solid #d9d9dd;color:#17171c"}"
                data-action="settings-loss-toggle">${r.daily_loss_limit != null ? "Enforced" : "Disabled"}</button>
            </div>
            <div style="display:flex;align-items:center;gap:12px;margin-top:14px">
              <span class="mono" style="font-size:18px">$</span>
              <input type="number" step="50" id="set-loss" value="${r.daily_loss_limit ?? ""}" ${r.daily_loss_limit == null ? "disabled" : ""}
                class="text-input" style="width:140px">
            </div>
            <p class="field-note">No new ENTRY/ADD once today's realized P&amp;L drops to this loss. TRIM/EXIT are never blocked.</p>
          </div>
        </div>
      </section>

      <div style="display:flex;flex-direction:column;gap:24px">
        <section class="dark-card">
          <h4>Discord &amp; classifier</h4>
          <p>Credentials stay on this machine and are never sent back to the browser. Leave a secret field blank to keep it unchanged. Requires a bot restart.</p>
          <div class="dark-grid">
            <div class="full"><div class="field-label">Auth token</div>
              <input type="password" id="set-discord-token" placeholder="${d.discord.user_token_set ? "•••••••••••••••• (set)" : "not set"}" class="text-input"></div>
            <div><div class="field-label">Channel ID</div>
              <input id="set-discord-channel" value="${escapeHtml(d.discord.channel_id ?? "")}" class="text-input mono"></div>
            <div><div class="field-label">User ID</div>
              <input id="set-discord-user" value="${escapeHtml(d.discord.casey_user_id ?? "")}" class="text-input mono"></div>
            <div class="full"><div class="field-label">LLM API key</div>
              <input type="password" id="set-llm-key" placeholder="${d.llm.api_key_set ? "•••••••••••••••• (set)" : "not set"}" class="text-input"></div>
            <div class="full"><div class="field-label">Model</div>
              <select id="set-llm-model" class="text-input mono">
                ${KNOWN_MODELS.map((m) => `
                  <option value="${m.id}" ${d.llm.model === m.id ? "selected" : ""}>${m.label} (${m.id})</option>
                `).join("")}
              </select>
            </div>
          </div>
        </section>
        <section class="outline-card">
          <h4>Interactive Brokers</h4>
          <div class="outline-grid">
            <div><div class="field-label" style="color:#75758a">Host</div><input id="set-ibkr-host" value="${escapeHtml(d.ibkr.host ?? "")}" class="text-input mono"></div>
            <div><div class="field-label" style="color:#75758a">Port</div><input id="set-ibkr-port" value="${escapeHtml(d.ibkr.port ?? "")}" class="text-input mono"></div>
            <div><div class="field-label" style="color:#75758a">Client ID</div><input id="set-ibkr-client" value="${escapeHtml(d.ibkr.client_id ?? "")}" class="text-input mono"></div>
          </div>
          <p class="restart-note">Requires a bot restart to take effect.</p>
        </section>
        <section class="outline-card">
          <h4>Reconnect</h4>
          <p class="field-note">What to do with a signal that arrived while IBKR was disconnected, once it reconnects.</p>
          <div style="display:flex;align-items:baseline;gap:12px;margin-top:8px">
            <span style="font-size:16px">Place after reconnect</span>
            <button class="btn-pill" style="margin-left:auto;${rc.retry_on_reconnect ? "background:#17171c;color:#fff" : "border:1px solid #d9d9dd;color:#17171c"}"
              data-action="settings-toggle-retry-reconnect">${rc.retry_on_reconnect ? "Enabled" : "Disabled"}</button>
          </div>
          <div style="display:flex;align-items:center;gap:12px;margin-top:14px">
            <span style="font-size:16px">Timeout <span style="font-size:14px;color:#616161">(min)</span></span>
            <input type="number" step="1" min="0" id="set-retry-timeout" value="${rc.retry_timeout_mins ?? 5}"
              ${rc.retry_on_reconnect ? "" : "disabled"} class="text-input" style="width:100px;margin-left:auto">
          </div>
          <p class="field-note">Always enforced while reconnect is enabled — a signal older than this when IBKR reconnects is discarded instead of placed.</p>
        </section>
      </div>
    </div>
    <button class="run-btn" id="settings-save-btn" style="align-self:flex-start" data-action="settings-save"
      ${isSettingsDirty() ? "" : "disabled"}>${isSettingsDirty() ? "Save to config.yaml" : "Saved ✓"}</button>
  `;
}

// Wires the settings form's plain inputs straight into ui.settingsDraft
// without ever re-rendering on keystroke (a re-render would fight the
// browser's own cursor position — see the module docstring). Pill/toggle
// buttons go through the normal data-action delegated click handler
// instead, since a full re-render on click is harmless for those.
function wireLiveInput(id, onInput) {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener("input", (e) => {
    onInput(e);
    updateSaveButtonState(); // every keystroke can change the dirty state
  });
}

// buildSettingsPayload is also what isSettingsDirty() compares against the
// saved baseline with — one definition of "what Save actually sends" for
// both, so the dirty indicator can never drift out of sync with what a
// click on Save would do.
function buildSettingsPayload(d) {
  return {
    risk: {
      sizing_mode: d.risk.sizing_mode || "fixed",
      max_contracts_per_trade: d.risk.max_contracts_per_trade,
      capital_per_trade: d.risk.capital_per_trade,
      trim_pct: d.risk.trim_pct,
      auto_submit_stop_loss: d.risk.auto_submit_stop_loss,
      add_pct: d.risk.add_pct,
      strike_offset: d.risk.strike_offset,
      expiry_selection: d.risk.expiry_selection,
      price_type: d.risk.price_type,
      daily_loss_limit: d.risk.daily_loss_limit,
      max_daily_losing_trades: d.risk.max_daily_losing_trades,
    },
    discord: {
      channel_id: d.discord.channel_id,
      casey_user_id: d.discord.casey_user_id,
      user_token: d.discord._token_input || undefined,
    },
    ibkr: { host: d.ibkr.host, port: +d.ibkr.port, client_id: +d.ibkr.client_id },
    llm: { model: d.llm.model, api_key: d.llm._key_input || undefined },
    reconnect: {
      retry_on_reconnect: d.reconnect.retry_on_reconnect,
      retry_timeout_mins: d.reconnect.retry_timeout_mins,
    },
  };
}

function isSettingsDirty() {
  if (!ui.settingsDraft || !ui.settingsSaved) return false;
  return JSON.stringify(buildSettingsPayload(ui.settingsDraft))
    !== JSON.stringify(buildSettingsPayload(ui.settingsSaved));
}

// Touches the Save button directly rather than re-rendering the form —
// called after every keystroke in a live-wired input, where a full
// rebuild would steal focus (see wireLiveInput's comment).
function updateSaveButtonState() {
  const btn = document.getElementById("settings-save-btn");
  if (!btn) return;
  const dirty = isSettingsDirty();
  btn.disabled = !dirty;
  btn.textContent = dirty ? "Save to config.yaml" : "Saved ✓";
}

function wireSettingsInputs() {
  wireLiveInput("set-size", (e) => {
    ui.settingsDraft.risk.max_contracts_per_trade = +e.target.value;
    document.getElementById("set-size-val").textContent = e.target.value;
  });
  wireLiveInput("set-capital-per-trade", (e) => { ui.settingsDraft.risk.capital_per_trade = +e.target.value; });
  wireLiveInput("set-trim", (e) => {
    ui.settingsDraft.risk.trim_pct = +e.target.value;
    document.getElementById("set-trim-val").textContent = e.target.value + "%";
  });
  wireLiveInput("set-add", (e) => {
    ui.settingsDraft.risk.add_pct = +e.target.value;
    document.getElementById("set-add-val").textContent = e.target.value + "%";
  });
  wireLiveInput("set-loss", (e) => { ui.settingsDraft.risk.daily_loss_limit = +e.target.value; });
  wireLiveInput("set-max-losing-trades", (e) => { ui.settingsDraft.risk.max_daily_losing_trades = +e.target.value; });
  wireLiveInput("set-discord-channel", (e) => { ui.settingsDraft.discord.channel_id = e.target.value; });
  wireLiveInput("set-discord-user", (e) => { ui.settingsDraft.discord.casey_user_id = e.target.value; });
  wireLiveInput("set-discord-token", (e) => { ui.settingsDraft.discord._token_input = e.target.value; });
  wireLiveInput("set-llm-key", (e) => { ui.settingsDraft.llm._key_input = e.target.value; });
  wireLiveInput("set-llm-model", (e) => { ui.settingsDraft.llm.model = e.target.value; });
  wireLiveInput("set-ibkr-host", (e) => { ui.settingsDraft.ibkr.host = e.target.value; });
  wireLiveInput("set-ibkr-port", (e) => { ui.settingsDraft.ibkr.port = +e.target.value; });
  wireLiveInput("set-ibkr-client", (e) => { ui.settingsDraft.ibkr.client_id = +e.target.value; });
  wireLiveInput("set-retry-timeout", (e) => { ui.settingsDraft.reconnect.retry_timeout_mins = +e.target.value; });

  const tickerInput = document.getElementById("new-ticker-input");
  if (tickerInput) {
    tickerInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") addTicker();
    });
  }
}

// allowed_tickers is deliberately excluded from buildSettingsPayload: it's
// owned exclusively by the dedicated add-ticker/remove-ticker endpoints
// now, applied immediately rather than batched here. Including a snapshot
// captured when this draft was initialized would risk this Save silently
// reverting a ticker add/remove that happened later, while the rest of the
// form was still being edited.
//
// channel_id/casey_user_id are sent as strings on purpose — these are
// 18-19 digit Discord snowflake IDs, past what a JS Number can hold
// exactly; the server does int() on these (arbitrary-precision in Python,
// no loss). Coercing with `+` here would reintroduce that rounding bug.
async function saveSettings() {
  const body = buildSettingsPayload(ui.settingsDraft);
  ui.settingsDraft = null; // force a fresh pull of the saved values
  ui.settingsSaved = null;
  await postJSON("/api/settings", body);
}

async function addTicker() {
  const input = document.getElementById("new-ticker-input");
  const btn = document.getElementById("add-ticker-btn");
  const status = document.getElementById("ticker-add-status");
  const ticker = input.value.trim().toUpperCase();
  if (!ticker) return;

  input.disabled = true;
  btn.disabled = true;
  status.style.color = "#75758a";
  status.textContent = `Validating ${ticker} with IBKR…`;

  try {
    const r = await fetch("/api/tickers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker }),
    });
    const data = await r.json();
    if (data.ok) {
      input.value = "";
      status.textContent = "";
      await fetchState(); // picks up the new ticker via renderTickerChips
    } else {
      status.style.color = "#b30000";
      status.textContent = data.reason || "Could not add ticker.";
    }
  } catch (e) {
    status.style.color = "#b30000";
    status.textContent = "Request failed — is the bot running?";
  } finally {
    input.disabled = false;
    btn.disabled = false;
  }
}

// ── right rail ──

function renderAside() {
  const events = state.feed.slice(0, 12);
  document.getElementById("aside").innerHTML = `
    <div class="aside-head">
      <div class="date display">${new Date().toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}</div>
      <div class="session">Regular session</div>
    </div>
    <div class="week-strip">
      ${state.week.map((d) => {
        const pnlColor = d.pnl == null ? (d.is_today ? "rgba(255,255,255,.8)" : "#93939f") : (d.is_today ? "rgba(255,255,255,.85)" : moneyColor(d.pnl));
        return `
        <div class="week-day ${d.is_today ? "today" : ""}">
          <div class="dow">${d.label.split(" ")[0]}</div>
          <div class="num mono">${d.label.split(" ")[1]}</div>
          <div class="pnl mono" style="color:${pnlColor}">${d.pnl == null ? "—" : fmtMoney(d.pnl)}</div>
        </div>
      `;
      }).join("")}
    </div>
    <div class="timeline-head">
      <div class="kicker">Recent events</div>
      <span class="count">${state.feed.length} events</span>
    </div>
    <div class="timeline">
      ${events.length ? events.map(renderEventCard).join("") : '<div class="empty">Nothing yet.</div>'}
    </div>
  `;
}

function renderEventCard(r) {
  const c = COLORS[r.type] || COLORS.NOISE;
  const line = r.type === "NOISE" ? "Logged, no order." : escapeHtml(r.outcome_text || r.blocked_reason || "Pending…");
  return `
    <div class="event-card">
      <div class="top">
        <span class="dot" style="background:${c.fg}"></span>
        <span class="action" style="color:${c.fg}">${r.type}</span>
        <span class="instrument">${escapeHtml(r.outcome_instrument || r.ticker || "")}</span>
        <span class="time">${fmtTime(r.ts)}</span>
      </div>
      <div class="line">${line}</div>
    </div>
  `;
}

// ── boot ──

initShell();
fetchState();
setInterval(fetchState, POLL_MS);
