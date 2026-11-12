"use strict";

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Prices in won or yen run to five or six digits; decimals there are noise.
const fmt = (v, d) => {
  if (v == null) return "–";
  d = d ?? (Math.abs(v) >= 10000 ? 0 : 2);
  return Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
};
const pct = (v, d = 2) => (v == null ? "–" : `${v > 0 ? "+" : ""}${fmt(v, d)}%`);
const sign = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
const big = (v) => (v == null ? "–" : v >= 1e12 ? fmt(v / 1e12) + "T" : v >= 1e9 ? fmt(v / 1e9) + "B"
  : v >= 1e6 ? fmt(v / 1e6) + "M" : fmt(v, 0));

const state = { ticker: null, status: null, busy: false, charts: null,
  currency: "EUR", rates: {}, k: 1, unit: "" };

// Display currency. Stocks are analyzed in their own trading currency; the dashboard converts
// every price with the latest exchange rate. `state.k` is the factor for the open stock.
const rateFor = (cur) => (cur && state.rates[cur] != null ? state.rates[cur] : null);
const money = (v, d) => fmt(v == null ? null : v * state.k, d);

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}

// --- header: status, provider, schedule ------------------------------------

async function refreshStatus() {
  const s = await api("/api/status");
  const wasBusy = state.busy;
  state.status = s;
  state.busy = s.running || s.analyzing.length > 0;

  const select = $("provider");
  if (!select.options.length) {
    for (const [name, p] of Object.entries(s.providers)) {
      const label = PROVIDER_LABELS[name] || name;
      select.add(new Option(p.installed ? label : `${label} – CLI not found`, name));
    }
  }
  if (document.activeElement !== select) select.value = s.provider;
  const cur = $("currency");
  if (!cur.options.length) for (const c of s.currencies) cur.add(new Option(c, c));
  if (document.activeElement !== cur) cur.value = state.currency;
  if (document.activeElement !== $("sched-time")) $("sched-time").value = s.schedule.time.padStart(5, "0");
  $("sched-on").checked = s.schedule.enabled;
  $("next-run").textContent = s.schedule.next_run
    ? `next ${s.schedule.next_run.replace("T", " ").slice(0, 16)}` : "schedule off";

  $("run-now").disabled = state.busy;
  $("analyze").disabled = state.busy;
  $("run-now").textContent = s.running ? "Running…" : "Run now";

  const banner = $("banner");
  const last = s.last_run;
  if (s.running && last) {
    banner.hidden = false; banner.className = "";
    banner.textContent = `Daily run in progress (${last.provider}): ${last.progress || ""}`;
  } else if (s.analyzing.length) {
    banner.hidden = false; banner.className = "";
    banner.textContent = `Analyzing ${s.analyzing.join(", ")}…`;
  } else if (last && (last.status === "failed" || last.status === "partial")) {
    banner.hidden = false; banner.className = "bad";
    const lines = (last.log || "").split("\n").filter((l) => l.includes("FAILED"));
    banner.textContent = `Last run ${last.status}: ${lines[0] || ""}` +
      (lines.length > 1 ? ` (+${lines.length - 1} more, see Runs)` : "");
  } else {
    banner.hidden = true;
  }

  const err = state.ticker && s.errors[state.ticker];
  $("s-error").hidden = !err;
  $("s-error").textContent = err || "";

  if (wasBusy && !state.busy) await refreshAll();
  return s;
}

$("provider").addEventListener("change", async (e) => {
  await api("/api/settings", { method: "PUT", body: JSON.stringify({ provider: e.target.value }) });
  await refreshAuth().catch(() => {});
  if (!$("view-settings").hidden) renderSettings();
  refreshStatus();
});

async function refreshRates() {
  const r = await api("/api/fx");
  state.currency = r.to;
  state.rates = r.rates;
  $("currency").value = r.to;
}

$("currency").addEventListener("change", async (e) => {
  await api("/api/settings", { method: "PUT", body: JSON.stringify({ currency: e.target.value }) });
  e.target.blur();
  refreshAll();
});

// --- providers: sign-in, model and effort (Settings tab) ---------------------

const PROVIDER_LABELS = { claude: "Claude (Anthropic)", codex: "GPT (Codex)" };
// pending: provider -> {url, since}; errors: provider -> message
const auth = { status: {}, pending: {}, errors: {}, custom: {} };

// Header: just a status badge for the active provider; the controls live in Settings.
function renderAuth() {
  const s = auth.status[$("provider").value];
  const box = $("auth");
  if (!s) { box.innerHTML = ""; return; }
  const choice = [s.model, s.effort].filter(Boolean).join(" · ");
  box.innerHTML = (s.logged_in
    ? `<span class="badge hit" title="${esc(s.detail)}">Signed in</span>`
    : `<button id="open-settings">${s.installed ? "Sign in" : "CLI not installed"}</button>`)
    + (choice ? ` <span class="muted">${esc(choice)}</span>` : "");
}

$("auth").addEventListener("click", (e) => {
  if (e.target.id === "open-settings") showView("settings");
});

function effortsFor(p, model) {
  const listed = p.models.find((m) => m.id === model);
  return listed ? listed.efforts : p.custom_efforts;
}

function authControls(name, p) {
  const pending = auth.pending[name];
  const error = auth.errors[name] ? `<span class="error">${esc(auth.errors[name])}</span>` : "";
  if (!p.installed) return `<span class="error">The ${esc(name)} CLI is not installed on this PC.</span>`;
  if (pending && !p.logged_in) {
    const link = pending.url && pending.url.startsWith("https://")
      ? `<a href="${esc(pending.url)}" target="_blank" rel="noopener">Open authorize page</a>` : "";
    return `<span class="muted">Waiting for sign-in in your browser…</span> ${link}
      <form class="code-form"><input class="sign-in-code" placeholder="Code, if the page shows one"
        autocomplete="off" spellcheck="false"><button>Submit</button></form>
      <button class="sign-in">Restart</button> ${error}`;
  }
  return `<button class="sign-in">${p.logged_in ? "Sign in again" : "Sign in"}</button> ${error}`;
}

function renderSettings() {
  const view = $("view-settings");
  const cards = Object.entries(auth.status).map(([name, p]) => {
    const listed = p.models.some((m) => m.id === p.model);
    const custom = auth.custom[name] || (p.model !== "" && !listed);
    const efforts = effortsFor(p, p.model);
    const option = (value, label, selected) =>
      `<option value="${esc(value)}"${selected ? " selected" : ""}>${esc(label)}</option>`;
    return `
    <div class="card provider" data-provider="${esc(name)}">
      <div class="analysis-head">
        <strong>${esc(PROVIDER_LABELS[name] || name)}</strong>
        <span class="badge ${p.logged_in ? "hit" : "miss"}">${esc(p.detail)}</span>
        <label class="use"><input type="radio" name="active-provider" ${p.active ? "checked" : ""}> Use for analysis</label>
      </div>
      <div class="field"><span class="field-name">Authorization</span>
        <div class="field-body">${authControls(name, p)}</div></div>
      <div class="field"><span class="field-name">Model</span>
        <div class="field-body">
          <select class="p-model">
            ${option("", "CLI default", !custom && p.model === "")}
            ${p.models.map((m) => option(m.id, m.label === m.id ? m.id : `${m.label} (${m.id})`, !custom && m.id === p.model)).join("")}
            ${option("__custom__", "Custom…", custom)}
          </select>
          <input class="p-custom" placeholder="exact model name" value="${custom ? esc(p.model) : ""}"
            autocomplete="off" spellcheck="false" ${custom ? "" : "hidden"}>
        </div></div>
      <div class="field"><span class="field-name">Effort</span>
        <div class="field-body">
          <select class="p-effort">
            ${option("", "Default", p.effort === "")}
            ${efforts.map((e) => option(e, e, e === p.effort)).join("")}
          </select>
          <span class="muted">Higher effort reasons longer and uses more of your subscription quota.</span>
        </div></div>
    </div>`;
  });
  view.innerHTML = cards.join("") || `<p class="muted">Loading providers…</p>`;
  view.dataset.sig = settingsSig();
}

// What the Settings cards depend on; re-render only when this changes, so polling never
// wipes an open dropdown or a half-typed code.
const settingsSig = () => JSON.stringify([auth.status, Object.keys(auth.pending), auth.errors, auth.custom]);

async function refreshAuth(force = false) {
  auth.status = await api(`/api/providers${force ? "?refresh=true" : ""}`);
  for (const name of Object.keys(auth.pending)) {
    const expired = Date.now() - auth.pending[name].since > 10 * 60 * 1000;
    if (auth.status[name]?.logged_in || expired) delete auth.pending[name];
  }
  renderAuth();
  const view = $("view-settings");
  if (!view.hidden && view.dataset.sig !== settingsSig() && !view.contains(document.activeElement)) {
    renderSettings();
  }
}

async function signIn(name) {
  delete auth.errors[name];
  try {
    const started = await api(`/api/providers/${name}/login`, { method: "POST" });
    if (!started.url) {
      // The CLI did not print an authorize link (it may already be signed in, or failed).
      auth.errors[name] = started.output || `Could not start sign-in. Try \`${started.login_command}\` in a terminal.`;
    } else {
      auth.pending[name] = { url: started.url, since: Date.now() };
    }
  } catch (e) {
    auth.errors[name] = e.message;
  }
  await refreshAuth(true).catch(() => {});
  renderSettings();
}

async function saveChoice(card) {
  const name = card.dataset.provider;
  const p = auth.status[name];
  const picked = card.querySelector(".p-model").value;
  auth.custom[name] = picked === "__custom__";
  const model = auth.custom[name] ? card.querySelector(".p-custom").value.trim() : picked;
  if (auth.custom[name] && !model) {      // wait for the name to be typed
    card.querySelector(".p-custom").hidden = false;
    card.querySelector(".p-custom").focus();
    return;
  }
  let effort = card.querySelector(".p-effort").value;
  if (!effortsFor(p, model).includes(effort)) effort = "";  // not offered by the new model
  delete auth.errors[name];
  try {
    await api(`/api/providers/${name}`, { method: "PUT", body: JSON.stringify({ model, effort }) });
  } catch (e) {
    auth.errors[name] = e.message;
  }
  await refreshAuth().catch(() => {});
  renderSettings();
}

$("view-settings").addEventListener("click", (e) => {
  const card = e.target.closest(".provider");
  if (card && e.target.classList.contains("sign-in")) {
    e.target.disabled = true;
    e.target.textContent = "Opening the authorize page…";
    signIn(card.dataset.provider);
  }
});

$("view-settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  const card = e.target.closest(".provider");
  const name = card.dataset.provider;
  const code = card.querySelector(".sign-in-code").value.trim();
  if (!code) return;
  delete auth.errors[name];
  try {
    await api(`/api/providers/${name}/login/code`, { method: "POST", body: JSON.stringify({ code }) });
  } catch (err) {
    auth.errors[name] = err.message;
  }
  await refreshAuth(true).catch(() => {});
  renderSettings();
});

$("view-settings").addEventListener("change", async (e) => {
  const card = e.target.closest(".provider");
  if (!card) return;
  if (e.target.name === "active-provider") {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ provider: card.dataset.provider }) });
    $("provider").value = card.dataset.provider;
    await refreshAuth().catch(() => {});
    renderSettings();
    refreshStatus();
  } else if (e.target.matches(".p-model, .p-effort, .p-custom")) {
    saveChoice(card);
  }
});

setInterval(() => {
  // Poll quickly while a sign-in is underway, otherwise just keep the badges fresh.
  if (Object.keys(auth.pending).length) refreshAuth(true).catch(() => {});
}, 4000);
setInterval(() => refreshAuth().catch(() => {}), 60000);

async function saveSchedule() {
  const time = $("sched-time").value;
  if (!time) return;
  await api("/api/schedule", {
    method: "PUT",
    body: JSON.stringify({ enabled: $("sched-on").checked, time: time.slice(0, 5), weekdays_only: true }),
  });
  refreshStatus();
}
$("sched-time").addEventListener("change", saveSchedule);
$("sched-on").addEventListener("change", saveSchedule);

$("run-now").addEventListener("click", async () => {
  try { await api("/api/run", { method: "POST" }); } catch (e) { alert(e.message); }
  refreshStatus();
});

// --- watchlist --------------------------------------------------------------

function sparkline(values) {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const pts = values.map((v, i) =>
    `${(i / (values.length - 1) * 68 + 1).toFixed(1)},${(23 - (v - min) / span * 22).toFixed(1)}`).join(" ");
  const color = values.at(-1) >= values[0] ? "var(--up)" : "var(--down)";
  return `<svg width="70" height="24" viewBox="0 0 70 24"><polyline fill="none" stroke="${color}" stroke-width="1.5" points="${pts}"/></svg>`;
}

async function refreshWatchlist() {
  const list = await api("/api/watchlist");
  $("watchlist").innerHTML = list.map((w) => {
    const k = rateFor(w.currency);
    return `
    <li data-ticker="${esc(w.ticker)}" class="${w.ticker === state.ticker ? "active" : ""}">
      <div style="min-width:0">
        <div class="w-ticker">${esc(w.ticker)}
          ${w.trend ? `<span class="badge ${esc(w.trend)}">${esc(w.trend)}</span>` : ""}</div>
        <div class="w-name">${esc(w.name || w.exchange || "")}</div>
      </div>
      ${sparkline(w.spark)}
      <div class="w-right">${fmt(w.close == null ? null : w.close * (k ?? 1))}
        <span class="unit">${esc(k != null ? state.currency : w.currency || "")}</span>
        <small class="${sign(w.change_pct)}">${pct(w.change_pct)}</small></div>
    </li>`; }).join("") || `<li class="muted">Watchlist is empty.</li>`;
  if (!state.ticker && list.length) selectTicker(list[0].ticker);
}

$("watchlist").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-ticker]");
  if (li) selectTicker(li.dataset.ticker);
});

// Company-name search: Korean and Japanese listings have numeric codes nobody remembers.
let searchTimer = null, searchSeq = 0;
function hideSuggestions() { $("suggestions").hidden = true; $("suggestions").innerHTML = ""; }

$("add-input").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = $("add-input").value.trim();
  if (q.length < 2) { hideSuggestions(); return; }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    const found = await api(`/api/search?q=${encodeURIComponent(q)}`).catch(() => []);
    if (seq !== searchSeq || $("add-input").value.trim() !== q) return;  // stale answer
    $("suggestions").innerHTML = found.map((f) => `
      <li data-ticker="${esc(f.ticker)}"><strong>${esc(f.ticker)}</strong>
        <small>${esc(f.name)} · ${esc(f.exchange)}${f.type !== "equity" ? " · " + esc(f.type) : ""}</small></li>`).join("");
    $("suggestions").hidden = !found.length;
  }, 300);
});

$("suggestions").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-ticker]");
  if (!li) return;
  $("add-input").value = li.dataset.ticker;
  $("add-form").requestSubmit();
});

$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("add-input"), error = $("add-error");
  const ticker = input.value.trim();
  if (!ticker) return;
  clearTimeout(searchTimer); searchSeq++; hideSuggestions();
  error.hidden = true;
  input.disabled = true;
  try {
    const added = await api("/api/watchlist", { method: "POST", body: JSON.stringify({ ticker }) });
    input.value = "";
    state.ticker = added.ticker;
    await refreshAll();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    input.disabled = false;
    input.focus();
  }
});

// --- charts -----------------------------------------------------------------

function buildCharts() {
  const base = {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: "#8a97aa", fontSize: 11 },
    grid: { vertLines: { color: "#1c2430" }, horzLines: { color: "#1c2430" } },
    rightPriceScale: { borderColor: "#263041", minimumWidth: 70 },
    timeScale: { borderColor: "#263041" },
    crosshair: { mode: 0 },
  };
  const LC = LightweightCharts;
  const price = LC.createChart($("chart-price"), base);
  const rsi = LC.createChart($("chart-rsi"), base);
  const macd = LC.createChart($("chart-macd"), base);
  const line = (chart, color, width = 1, extra = {}) => chart.addLineSeries(
    { color, lineWidth: width, priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false, ...extra });

  const c = {
    all: [price, rsi, macd],
    volume: price.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" },
      priceLineVisible: false, lastValueVisible: false }),
    bbU: line(price, "#6b7a90", 1, { lineStyle: 2 }),
    bbL: line(price, "#6b7a90", 1, { lineStyle: 2 }),
    sma200: line(price, "#b388ff"),
    sma50: line(price, "#4c9aff"),
    sma20: line(price, "#f5a524"),
    candles: price.addCandlestickSeries({ upColor: "#2ebd85", downColor: "#f6465d",
      borderVisible: false, wickUpColor: "#2ebd85", wickDownColor: "#f6465d" }),
    rsi: line(rsi, "#4c9aff", 1.5, { lastValueVisible: true }),
    macdHist: macd.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false }),
    macd: line(macd, "#4c9aff", 1.5),
    macdSig: line(macd, "#f5a524", 1.5),
    levels: [],
  };
  price.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  for (const level of [70, 30]) {
    c.rsi.createPriceLine({ price: level, color: "#3a465a", lineWidth: 1, lineStyle: 2, axisLabelVisible: true });
  }
  // Keep the three panes scrolled and zoomed together.
  let syncing = false;
  for (const chart of c.all) {
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (syncing || !range) return;
      syncing = true;
      for (const other of c.all) if (other !== chart) other.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    });
  }
  return c;
}

function drawCharts(d) {
  const c = state.charts || (state.charts = buildCharts());
  // Pad indicator series with empty points so all panes share one time axis.
  const k = state.k;
  const pad = (series, scale = true) => {
    const byTime = new Map(series.map((p) => [p.time, scale ? { ...p, value: p.value * k } : p]));
    return d.candles.map((k) => byTime.get(k.time) || { time: k.time });
  };
  c.candles.setData(d.candles.map((b) => ({ time: b.time, open: b.open * k, high: b.high * k,
    low: b.low * k, close: b.close * k })));
  c.volume.setData(d.volume.map((v) => ({ time: v.time, value: v.value,
    color: v.up ? "rgba(46,189,133,.35)" : "rgba(246,70,93,.35)" })));
  c.sma20.setData(pad(d.series.sma20 || []));
  c.sma50.setData(pad(d.series.sma50 || []));
  c.sma200.setData(pad(d.series.sma200 || []));
  c.bbU.setData(pad(d.series.bb_upper || []));
  c.bbL.setData(pad(d.series.bb_lower || []));
  c.rsi.setData(pad(d.series.rsi14 || [], false));
  c.macd.setData(pad(d.series.macd || []));
  c.macdSig.setData(pad(d.series.macd_signal || []));
  c.macdHist.setData(pad((d.series.macd_hist || []).map((p) => ({ ...p,
    color: p.value >= 0 ? "rgba(46,189,133,.6)" : "rgba(246,70,93,.6)" }))));

  for (const l of c.levels) c.candles.removePriceLine(l);
  c.levels = [];
  const a = d.analyses[0];
  if (a) {
    for (const [key, color] of [["support", "#2ebd85"], ["resistance", "#f6465d"]]) {
      if (a[key]) c.levels.push(c.candles.createPriceLine(
        { price: a[key] * k, color, lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: key }));
    }
  }
  const n = d.candles.length;
  c.all[0].timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 180), to: n + 3 });
}

// --- trade signals ----------------------------------------------------------

const OUTCOME_BADGE = { target: "target", stopped: "stopped", right: "right", wrong: "wrong" };

function signalSize(s, limits) {
  if (s.position_pct == null) return "–";
  const amount = limits && limits.portfolio_value > 0
    ? ` (≈ ${fmt(limits.portfolio_value * s.position_pct / 100, 0)} ${limits.portfolio_currency})` : "";
  return `${fmt(s.position_pct, 1)}% of portfolio${amount}`;
}

// Why a model proposal is shown as "hold" instead.
const downgradeNote = (s) => s.proposed_action === s.action ? ""
  : `Model proposed ${s.proposed_action}; shown as hold because ${s.checks.join("; ")}.`;

// Signal card on the stock page. Prices use the open stock's conversion (money()).
function signalBlock(s, limits) {
  if (!s) return `<div class="signal muted">No trade signal in this analysis. Use “Analyze now” to get one.</div>`;
  const note = downgradeNote(s);
  const tag = note ? " (proposed)" : "";
  const rows = [];
  const price = (v) => `${money(v)} ${state.unit}`;
  if (s.limit_price != null) rows.push([`${s.proposed_action === "sell" ? "Sell" : "Buy"} limit${tag}`, price(s.limit_price)]);
  if (s.stop_loss != null) rows.push([`Stop-loss${tag}`, price(s.stop_loss)]);
  if (s.take_profit != null) rows.push([`Take-profit${tag}`, price(s.take_profit)]);
  if (s.reward_risk != null) rows.push(["Reward / risk", fmt(s.reward_risk)]);
  if (s.position_pct != null) rows.push(["Position size", signalSize(s, limits)]);
  return `<div class="signal">
    <div class="analysis-head">
      <strong>Trade signal</strong>
      <span class="badge ${esc(s.action)}">${esc(s.action)}</span>
      <span class="muted">${Math.round(s.confidence * 100)}% confidence${s.horizon_days ? ` · ${s.horizon_days}-day horizon` : ""}</span>
    </div>
    ${rows.length ? `<div class="kv">${rows.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("")}</div>` : ""}
    ${note ? `<p style="color:var(--flat)">${esc(note)}</p>` : ""}
    ${s.reasoning ? `<p>${esc(s.reasoning)}</p>` : ""}
    ${s.action !== "hold" ? `<p class="muted note">Check the live price before ordering; you place the order yourself at your broker.</p>` : ""}
  </div>`;
}

async function renderSignals() {
  const s = await api("/api/signals");
  const L = s.limits;
  const price = (v, r) => (v == null ? "–" : fmt(v * (rateFor(r.currency) ?? 1)));
  const unit = (r) => (rateFor(r.currency) != null ? state.currency : r.currency || "");
  const stock = (r) => `<strong>${esc(r.ticker)}</strong> <span class="muted">${esc(r.name || "")}</span>`;

  const latest = s.latest.map((r) => {
    const g = r.signal;
    return `<tr data-ticker="${esc(r.ticker)}" style="cursor:pointer">
      <td>${stock(r)}</td>
      <td><span class="badge ${esc(g.action)}">${esc(g.action)}</span></td>
      <td class="num">${Math.round(g.confidence * 100)}%</td>
      <td class="num">${g.horizon_days ? g.horizon_days + "d" : "–"}</td>
      <td class="num">${price(r.close, r)} ${esc(unit(r))}</td>
      <td class="num">${price(g.limit_price, r)}</td>
      <td class="num">${price(g.stop_loss, r)}</td>
      <td class="num">${price(g.take_profit, r)}</td>
      <td class="num">${g.reward_risk == null ? "–" : fmt(g.reward_risk)}</td>
      <td>${esc(signalSize(g, L))}</td>
      <td>${esc(r.run_date)}</td>
      <td class="reason">${esc(downgradeNote(g) || g.reasoning)}</td>
    </tr>`; }).join("");

  const history = s.history.map((r) => {
    const g = r.signal, o = r.outcome;
    return `<tr>
      <td>${esc(r.run_date)}</td><td>${stock(r)}</td>
      <td><span class="badge ${esc(g.action)}">${esc(g.action)}</span></td>
      <td class="num">${g.horizon_days}d</td>
      <td class="num">${price(g.limit_price, r)} ${esc(unit(r))}</td>
      <td class="num">${price(g.stop_loss, r)}</td>
      <td class="num">${price(g.take_profit, r)}</td>
      <td><span class="badge ${OUTCOME_BADGE[o.status] || "pending"}">${esc(o.status)}</span></td>
      <td class="num ${sign(o.return_pct)}">${o.return_pct == null ? "–" : pct(o.return_pct)}</td>
    </tr>`; }).join("");

  const m = s.summary;
  const record = [
    m.buys_finished ? `${m.buys_won} of ${m.buys_finished} finished buy signals ended with a gain, average return ${pct(m.buy_avg_return_pct)}.` : "",
    m.sells_finished ? `${m.sells_right} of ${m.sells_finished} sell signals were followed by a lower close.` : "",
  ].filter(Boolean).join(" ") || "No signal has finished yet.";

  $("view-signals").innerHTML = `
    <div class="card"><h3>Latest signal per stock</h3>
      ${latest ? `<div class="table-wrap"><table>
        <tr><th>Stock</th><th>Signal</th><th class="num">Conf.</th><th class="num">Horizon</th>
        <th class="num">Last close</th><th class="num">Limit</th><th class="num">Stop-loss</th>
        <th class="num">Take-profit</th><th class="num">Reward / risk</th><th>Position size</th>
        <th>As of</th><th>Why</th></tr>${latest}</table></div>`
      : `<p class="muted">No signals yet. Analyses made before signals were added carry none; use “Run now”.</p>`}
      <p class="muted note">A buy or sell from the model is shown only if it passes these limits, otherwise it becomes hold:
        confidence at least ${fmt(L.min_confidence * 100, 0)}%, limit within ${fmt(L.max_entry_gap_pct, 1)}% of the last close,
        agreement with the forecast for its horizon; for a buy also a stop-loss at most ${fmt(L.max_stop_pct, 1)}% below the limit
        and reward / risk of at least ${fmt(L.min_reward_risk, 1)}. Position size risks ${fmt(L.risk_per_trade_pct, 1)}% of the
        portfolio at the stop, capped at ${fmt(L.max_position_pct, 0)}% per stock. Change them in config.yaml.</p>
      <p class="muted note">Sell means exit or reduce a position you hold. Nothing here places an order: check the live price and enter the order yourself at your broker.</p>
    </div>
    <div class="card"><h3>Past buy and sell signals</h3>
      ${history ? `<div class="table-wrap"><table>
        <tr><th>Date</th><th>Stock</th><th>Signal</th><th class="num">Horizon</th><th class="num">Limit</th>
        <th class="num">Stop-loss</th><th class="num">Take-profit</th><th>Outcome</th><th class="num">Return</th></tr>
        ${history}</table></div>` : `<p class="muted">No buy or sell signals yet.</p>`}
      <p class="muted note">${esc(record)} Outcomes are simulated on daily bars: a buy fills when the low reaches the limit and ends at the
        stop-loss, the take-profit or the close of the horizon's last day. Fees, taxes and price gaps are ignored.</p>
    </div>`;
}

$("view-signals").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-ticker]");
  if (!row) return;
  showView("stocks");
  selectTicker(row.dataset.ticker);
});

// --- stock detail -----------------------------------------------------------

function renderAnalysis(d) {
  const a = d.analyses[0];
  if (!a) {
    $("analysis").innerHTML = `<h3>AI analysis</h3><p class="muted">No analysis yet. Use “Analyze now” or wait for the daily run.</p>`;
    return;
  }
  const forecasts = a.predictions.map((p) => `
    <div class="forecast ${esc(p.direction)}">
      <div class="h">${p.horizon_days} trading day${p.horizon_days > 1 ? "s" : ""}${p.due_date ? ` · ~${esc(p.due_date)}` : ""}</div>
      <div class="d">${esc(p.direction)}</div>
      <div class="bar"><i style="width:${Math.round(p.confidence * 100)}%"></i></div>
      <div class="muted">${Math.round(p.confidence * 100)}% confidence</div>
      <div style="color:var(--text)">${money(p.target_low)} – ${money(p.target_high)} ${esc(state.unit)}</div>
    </div>`).join("");
  const list = (items) => items.length ? `<ul>${items.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : `<p class="muted">–</p>`;
  $("analysis").innerHTML = `
    <div class="analysis-head">
      <h3 style="margin:0">AI analysis</h3>
      <span class="badge ${esc(a.trend)}">${esc(a.trend)}</span>
      <span class="muted">${esc(a.run_date)} · ${esc(a.provider)} / ${esc(a.model)}${a.effort ? " · " + esc(a.effort) : ""}</span>
    </div>
    <div class="forecasts">${forecasts}</div>
    ${signalBlock(a.signal, d.signal_limits)}
    <p>${esc(a.summary)}</p>
    <h3>Key signals</h3>${list(a.key_signals)}
    <h3>Risks</h3>${list(a.risks)}
    <div class="muted">Support ${money(a.support)} · Resistance ${money(a.resistance)} ${esc(state.unit)}</div>
    ${state.unit && d.currency && state.unit !== d.currency ? `<p class="muted note">Prices are converted from ${esc(d.currency)} to ${esc(state.unit)} at the latest exchange rate. The model works in ${esc(d.currency)}, so figures quoted inside its text are in ${esc(d.currency)}.</p>` : ""}`;
}

function renderIndicators(d) {
  const s = d.snapshot;
  if (!s) { $("indicators").innerHTML = `<h3>Indicators</h3><p class="muted">No price data.</p>`; return; }
  const rows = [
    [`Close (${state.unit})`, money(s.close)], ["1 day", pct(s.change_1d_pct)], ["5 days", pct(s.change_5d_pct)],
    ["20 days", pct(s.change_20d_pct)], ["RSI 14", fmt(s.rsi14, 1)],
    ["MACD / signal", `${money(s.macd)} / ${money(s.macd_signal)}`],
    ["SMA 20 / 50 / 200", `${money(s.sma20)} / ${money(s.sma50)} / ${money(s.sma200)}`],
    ["Bollinger", `${money(s.bb_lower)} – ${money(s.bb_upper)}`],
    ["ATR 14", `${money(s.atr14)} (${fmt(s.atr_pct)}%)`],
    ["Stochastic %K / %D", `${fmt(s.stoch_k, 0)} / ${fmt(s.stoch_d, 0)}`],
    ["Volume vs 20d avg", s.volume_ratio_20d == null ? "–" : `${fmt(s.volume_ratio_20d)}x (${big(s.volume)})`],
    ["52-week range", `${money(s.low_52w)} – ${money(s.high_52w)}`],
  ];
  $("indicators").innerHTML = `<h3>Indicators</h3>
    <div class="kv">${rows.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("")}</div>
    <h3 style="margin-top:14px">Signals</h3>
    <ul>${s.signals.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
}

function renderHistory(d) {
  const rows = d.analyses.flatMap((a) => a.predictions.map((p) => `
    <tr>
      <td>${esc(a.run_date)}</td><td>${esc(a.provider)}</td><td class="num">${p.horizon_days}d</td>
      <td><span class="badge ${esc(p.direction)}">${esc(p.direction)}</span></td>
      <td class="num">${Math.round(p.confidence * 100)}%</td>
      <td class="num">${money(p.base_close)}</td>
      <td class="num">${money(p.target_low)} – ${money(p.target_high)}</td>
      <td class="num">${p.actual_close == null ? "–" : money(p.actual_close)}</td>
      <td class="num ${sign(p.actual_return)}">${p.actual_return == null ? "–" : pct(p.actual_return * 100)}</td>
      <td><span class="badge ${p.outcome || "pending"}">${p.outcome || "pending"}</span></td>
      <td>${p.in_range == null ? "–" : p.in_range ? "yes" : "no"}</td>
    </tr>`));
  $("history").innerHTML = rows.length ? `
    <tr><th>Date</th><th>Model</th><th class="num">Horizon</th><th>Call</th><th class="num">Conf.</th>
    <th class="num">From (${esc(state.unit)})</th><th class="num">Target range</th><th class="num">Actual</th>
    <th class="num">Return</th><th>Direction</th><th>In range</th></tr>${rows.join("")}`
    : `<tr><td class="muted">No forecasts yet.</td></tr>`;
}

async function loadStock() {
  if (!state.ticker) return;
  let d;
  try {
    d = await api(`/api/stocks/${encodeURIComponent(state.ticker)}`);
  } catch {
    state.ticker = null;
    $("stock").hidden = true; $("empty").hidden = false;
    return;
  }
  $("empty").hidden = true; $("stock").hidden = false;
  $("s-title").textContent = `${d.ticker}${d.name ? " · " + d.name : ""}`;
  const tv = $("tv-link");
  tv.hidden = !(d.tradingview_url && d.tradingview_url.startsWith("https://www.tradingview.com/"));
  if (!tv.hidden) tv.href = d.tradingview_url;
  const s = d.snapshot;
  const k = rateFor(d.currency);
  state.k = k ?? 1;
  state.unit = k != null ? state.currency : (d.currency || "");
  const converted = d.currency && state.unit !== d.currency;
  $("s-sub").innerHTML = [esc(d.exchange || ""),
    s ? `${money(s.close)} ${esc(state.unit)} <span class="${sign(s.change_1d_pct)}">${pct(s.change_1d_pct)}</span>` : "",
    s && converted ? `${fmt(s.close)} ${esc(d.currency)}` : "",
    d.candles.length ? `as of ${esc(d.candles.at(-1).time)}` : ""]
    .filter(Boolean).join(" · ");
  if (d.candles.length) drawCharts(d);
  renderAnalysis(d); renderIndicators(d); renderHistory(d);
}

function selectTicker(ticker) {
  state.ticker = ticker;
  for (const li of document.querySelectorAll("#watchlist li")) {
    li.classList.toggle("active", li.dataset.ticker === ticker);
  }
  $("s-error").hidden = true;
  loadStock();
}

$("analyze").addEventListener("click", async () => {
  try { await api(`/api/analyze/${encodeURIComponent(state.ticker)}`, { method: "POST" }); }
  catch (e) { alert(e.message); }
  refreshStatus();
});

$("remove").addEventListener("click", async () => {
  if (!confirm(`Remove ${state.ticker} and its stored analyses?`)) return;
  await api(`/api/watchlist/${encodeURIComponent(state.ticker)}`, { method: "DELETE" });
  state.ticker = null;
  $("stock").hidden = true; $("empty").hidden = false;
  refreshWatchlist();
});

// --- accuracy and runs views ------------------------------------------------

async function renderAccuracy() {
  const a = await api("/api/accuracy");
  const table = (rows, first, key) => rows.length ? `<div class="table-wrap"><table>
    <tr><th>Model</th><th>${first}</th><th class="num">Graded</th><th class="num">Direction hit rate</th>
    <th class="num">Close in target range</th><th class="num">Avg. confidence</th></tr>
    ${rows.map((r) => `<tr><td>${esc(r.provider)}</td><td>${esc(key(r))}</td><td class="num">${r.graded}</td>
      <td class="num">${fmt(r.hit_rate * 100, 0)}%</td><td class="num">${fmt(r.range_rate * 100, 0)}%</td>
      <td class="num">${fmt(r.avg_confidence * 100, 0)}%</td></tr>`).join("")}</table></div>`
    : `<p class="muted">No graded forecasts yet. A forecast is graded once its horizon has passed.</p>`;
  $("view-accuracy").innerHTML = `
    <div class="card"><h3>By horizon</h3>${table(a.by_horizon, "Horizon", (r) => r.horizon_days + " days")}</div>
    <div class="card"><h3>By stock</h3>${table(a.by_ticker, "Ticker", (r) => r.ticker)}</div>
    <p class="muted">${a.pending} forecasts still pending. A well-calibrated model's hit rate is close to its average confidence.</p>`;
}

async function renderRuns() {
  const runs = await api("/api/runs");
  $("view-runs").innerHTML = runs.length ? runs.map((r) => `
    <div class="card">
      <div class="analysis-head"><strong>${esc(r.started_at.replace("T", " "))}</strong>
        <span class="badge ${r.status === "ok" ? "hit" : r.status === "running" ? "pending" : "miss"}">${esc(r.status)}</span>
        <span class="muted">${esc(r.trigger)} · ${esc(r.provider)} · ${esc(r.progress || "")}</span></div>
      <pre>${esc(r.log || "")}</pre>
    </div>`).join("") : `<p class="muted">No runs yet.</p>`;
}

function showView(name) {
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t.dataset.view === name);
  for (const view of ["stocks", "signals", "accuracy", "runs", "settings"]) $(`view-${view}`).hidden = view !== name;
  if (name === "signals") renderSignals();
  if (name === "accuracy") renderAccuracy();
  if (name === "runs") renderRuns();
  if (name === "settings") { renderSettings(); refreshAuth(true).then(renderSettings).catch(() => {}); }
}
for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => showView(tab.dataset.view));
}

// --- boot -------------------------------------------------------------------

async function refreshAll() {
  await refreshRates().catch(() => {});
  await refreshWatchlist();
  await loadStock();
  if (!$("view-signals").hidden) renderSignals();
  if (!$("view-accuracy").hidden) renderAccuracy();
  if (!$("view-runs").hidden) renderRuns();
}

(async function boot() {
  await refreshStatus().catch(() => {});
  refreshAuth().catch(() => {});
  await refreshAll();
  setInterval(() => refreshStatus().catch(() => {}), 3000);
})();
