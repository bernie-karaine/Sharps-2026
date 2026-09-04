const API = "/api";
let currentDrilldownId = null;
let currentRound = null; // null = "live" (is_monitored=true), else a round_label
let charts = [];

// ---------------------------------------------------------------------------
// Rounds
// ---------------------------------------------------------------------------

async function loadRounds(preferredLabel) {
  const rounds = await api("/rounds");
  const select = document.getElementById("round-select");
  const previous = preferredLabel !== undefined ? preferredLabel : select.value;

  select.innerHTML = rounds
    .map((r) => {
      const range = r.earliest_kickoff
        ? new Date(r.earliest_kickoff).toLocaleDateString()
        : "";
      return `<option value="${r.round_label}">${r.round_label} — ${r.match_count} matches (${range})${r.is_active ? " ● live" : ""}</option>`;
    })
    .join("");

  let target = previous;
  if (!target || !rounds.some((r) => r.round_label === target)) {
    const active = rounds.find((r) => r.is_active);
    target = active ? active.round_label : rounds[0] ? rounds[0].round_label : null;
  }
  if (target) select.value = target;
  currentRound = target;
  updateRoundStatus(rounds);
}

function updateRoundStatus(rounds) {
  const status = document.getElementById("round-status");
  const r = rounds.find((x) => x.round_label === currentRound);
  if (!r) {
    status.textContent = "";
    return;
  }
  status.textContent = r.is_active ? "● live — actively polling" : "○ archived — no longer polled";
  status.className = r.is_active ? "pill pill-ok" : "pill pill-dim";
}

document.getElementById("round-select").onchange = async (e) => {
  currentRound = e.target.value;
  const rounds = await api("/rounds");
  updateRoundStatus(rounds);
  loadSummary();
};

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function fmtPct(v) {
  return v === null || v === undefined ? "—" : `${v.toFixed(2)}%`;
}
function fmtPp(v) {
  return v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}pp`;
}
function fmtNum(v, d = 2) {
  return v === null || v === undefined ? "—" : Number(v).toFixed(d);
}
function toUnixSeconds(iso) {
  return Math.floor(new Date(iso).getTime() / 1000);
}
function toSeriesData(rows, valueField, timeField = "captured_at") {
  const out = [];
  let lastTime = null;
  for (const r of rows) {
    const v = r[valueField];
    if (v === null || v === undefined) continue;
    const t = toUnixSeconds(r[timeField]);
    if (t === lastTime) {
      out[out.length - 1].value = v; // last-write-wins for same-second dupes
      continue;
    }
    out.push({ time: t, value: v });
    lastTime = t;
  }
  return out;
}

async function api(path, options) {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : null;
}

// ---------------------------------------------------------------------------
// Summary table
// ---------------------------------------------------------------------------

function formatCountdown(startIso) {
  const diffMs = new Date(startIso).getTime() - Date.now();
  if (diffMs <= 0) return { text: "Kicked off", cls: "countdown-live" };
  const totalSec = Math.floor(diffMs / 1000);
  const days = Math.floor(totalSec / 86400);
  const hours = Math.floor((totalSec % 86400) / 3600);
  const mins = Math.floor((totalSec % 3600) / 60);
  const secs = totalSec % 60;
  let text;
  if (days > 0) text = `${days}d ${hours}h ${mins}m`;
  else if (hours > 0) text = `${hours}h ${mins}m`;
  else if (mins > 0) text = `${mins}m ${secs}s`;
  else text = `${secs}s`;
  const cls = totalSec <= 7200 ? "countdown-urgent" : "";
  return { text, cls };
}

function tickCountdowns() {
  document.querySelectorAll(".countdown[data-start]").forEach((el) => {
    const { text, cls } = formatCountdown(el.dataset.start);
    el.textContent = text;
    el.className = `countdown ${cls}`;
  });
}

async function loadSummary() {
  const qs = currentRound ? `?round_label=${encodeURIComponent(currentRound)}` : "";
  const rows = await api(`/matches/summary${qs}`);
  const body = document.getElementById("summary-body");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="12" class="dim">No matches in this round yet — use Discovery to add the Megajackpot fixtures.</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((r) => {
      const ah = r.ah_detail || {};
      const x2 = r.x2_detail || {};
      const lim = r.limit_detail || {};
      const tier = r.tier || "insufficient_data";
      const kickoff = new Date(r.start_time);
      return `
      <tr data-id="${r.id}">
        <td>${kickoff.toLocaleString()}</td>
        <td class="countdown" data-start="${r.start_time}">—</td>
        <td>${r.home_team} vs ${r.away_team}</td>
        <td class="dim">${r.league_name || ""}</td>
        <td><span class="tier-badge tier-${tier}">${tier.replace("_", " ")}</span></td>
        <td>${r.sharp_side || "—"}</td>
        <td>${fmtNum(r.total_score, 1)}</td>
        <td>${fmtPp(x2.home_pp !== undefined ? (Math.abs(x2.home_pp) >= Math.abs(x2.away_pp) ? x2.home_pp : x2.away_pp) : null)}</td>
        <td>${fmtPct(lim.moneyline_limit_drop_pct)}</td>
        <td>${ah.opening !== undefined && ah.opening !== null ? `${fmtNum(ah.opening)} → ${fmtNum(ah.current)}` : "—"}</td>
        <td>${ah.magnitude !== undefined ? fmtNum(ah.shift, 2) : "—"}</td>
        <td>${fmtPct(lim.spread_limit_drop_pct)}</td>
      </tr>`;
    })
    .join("");

  body.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.addEventListener("click", () => openDrilldown(Number(tr.dataset.id)));
  });
  tickCountdowns();
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

async function loadSettingsPanel() {
  const s = await api("/settings");
  document.getElementById("settings-current").textContent =
    `Current: arcadia=${s.arcadia_api_key || "(unset)"}  telegram_token=${s.telegram_bot_token || "(unset)"}  chat_id=${s.telegram_chat_id || "(unset)"}`;
}

document.getElementById("btn-settings").onclick = () => {
  document.getElementById("modal-settings").classList.remove("hidden");
  loadSettingsPanel();
};
document.getElementById("btn-settings-close").onclick = () =>
  document.getElementById("modal-settings").classList.add("hidden");

document.getElementById("btn-settings-save").onclick = async () => {
  const body = {};
  const key = document.getElementById("in-arcadia-key").value.trim();
  const tok = document.getElementById("in-tg-token").value.trim();
  const chat = document.getElementById("in-tg-chat").value.trim();
  if (key) body.arcadia_api_key = key;
  if (tok) body.telegram_bot_token = tok;
  if (chat) body.telegram_chat_id = chat;
  await api("/settings", { method: "POST", body: JSON.stringify(body) });
  document.getElementById("modal-settings").classList.add("hidden");
};

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

let discoveredMatches = [];

document.getElementById("btn-discovery").onclick = () => {
  document.getElementById("modal-discovery").classList.remove("hidden");
  loadSavedLeagues();
};
document.getElementById("btn-discovery-close").onclick = () =>
  document.getElementById("modal-discovery").classList.add("hidden");

async function loadSavedLeagues(checkIds) {
  const leagues = await api("/saved-leagues");
  const list = document.getElementById("saved-leagues-list");
  if (!leagues.length) {
    list.innerHTML = `<div class="league-row dim">No leagues saved yet - use "+ Add league" below.</div>`;
    return;
  }
  const toCheck = new Set(checkIds || []);
  list.innerHTML = leagues
    .map(
      (l) => `
      <div class="league-row">
        <label>
          <input type="checkbox" value="${l.league_id}" ${toCheck.has(l.league_id) ? "checked" : ""} />
          ${l.league_name} <span class="dim">#${l.league_id}</span>
        </label>
        <button class="league-remove" data-id="${l.league_id}" title="Remove from saved list">✕</button>
      </div>`
    )
    .join("");

  list.querySelectorAll(".league-remove").forEach((btn) => {
    btn.onclick = async () => {
      await api(`/saved-leagues/${btn.dataset.id}`, { method: "DELETE" });
      loadSavedLeagues();
    };
  });
}

document.getElementById("btn-toggle-add-league").onclick = () => {
  document.getElementById("add-league-form").classList.toggle("hidden");
};

document.getElementById("btn-save-league").onclick = async () => {
  const idInput = document.getElementById("in-new-league-id");
  const nameInput = document.getElementById("in-new-league-name");
  const league_id = Number(idInput.value.trim());
  const league_name = nameInput.value.trim();
  if (!league_id || !league_name) {
    alert("Both a numeric league ID and a name are required.");
    return;
  }
  await api("/saved-leagues", { method: "POST", body: JSON.stringify({ league_id, league_name }) });
  idInput.value = "";
  nameInput.value = "";
  document.getElementById("add-league-form").classList.add("hidden");
  await loadSavedLeagues([league_id]);
};

document.getElementById("btn-fetch-leagues").onclick = async () => {
  const ids = Array.from(document.querySelectorAll("#saved-leagues-list input[type=checkbox]:checked")).map(
    (el) => el.value
  );
  if (!ids.length) {
    document.getElementById("discovery-results").innerHTML = `<div class="dim">Tick at least one league above first.</div>`;
    return;
  }
  discoveredMatches = [];
  const container = document.getElementById("discovery-results");
  container.innerHTML = "Fetching…";
  try {
    for (const leagueId of ids) {
      const matches = await api(`/leagues/${leagueId}/matchups`);
      // The Arcadia /matchups list mixes real fixtures with their "special"
      // sub-markets (Draw No Bet, team props, etc.) - every special repeats
      // the same `parent` object holding the actual match. Resolve through
      // `.parent` (falling back to the entry itself when there's no parent,
      // i.e. this entry already IS the main fixture) and dedupe by that
      // resolved id, since many specials otherwise repeat the same match.
      const seen = new Map();
      for (const m of matches) {
        const main = m.parent || m;
        const participants = main.participants || [];
        const home = participants.find((p) => p.alignment === "home");
        const away = participants.find((p) => p.alignment === "away");
        if (!home || !away || seen.has(main.id)) continue;
        seen.set(main.id, {
          pinnacle_matchup_id: main.id,
          home_team: home.name,
          away_team: away.name,
          start_time: main.startTime,
          league_id: m.league?.id ?? Number(leagueId),
          league_name: m.league?.name ?? "",
        });
      }
      discoveredMatches.push(...seen.values());
    }
    container.innerHTML = discoveredMatches
      .map(
        (m, i) => `
        <label>
          <input type="checkbox" data-idx="${i}" />
          ${new Date(m.start_time).toLocaleString()} — ${m.home_team} vs ${m.away_team}
          <span class="dim">(${m.league_name})</span>
        </label>`
      )
      .join("");
  } catch (e) {
    container.innerHTML = `<div class="dim">Fetch failed: ${e.message}</div>`;
  }
};

document.getElementById("btn-add-selected").onclick = async () => {
  const checked = Array.from(document.querySelectorAll("#discovery-results input:checked")).map(
    (el) => discoveredMatches[Number(el.dataset.idx)]
  );
  if (!checked.length) return;
  const mjp_round_label = document.getElementById("in-round-label").value.trim() || null;
  await api("/monitored-matches", {
    method: "POST",
    body: JSON.stringify({ matches: checked, mjp_round_label }),
  });
  document.getElementById("modal-discovery").classList.add("hidden");
  await loadRounds(mjp_round_label || "(unlabeled)");
  loadSummary();
};

// ---------------------------------------------------------------------------
// Drill-down
// ---------------------------------------------------------------------------

function destroyCharts() {
  charts.forEach((c) => c.remove());
  charts = [];
}

function makeChart(elId) {
  const el = document.getElementById(elId);
  el.innerHTML = "";
  const chart = LightweightCharts.createChart(el, {
    width: el.clientWidth,
    height: el.clientHeight,
    layout: { background: { color: "#10140f" }, textColor: "#c9f0c0", fontFamily: "monospace" },
    grid: { vertLines: { color: "#263123" }, horzLines: { color: "#263123" } },
    rightPriceScale: { borderColor: "#263123" },
    leftPriceScale: { visible: false, borderColor: "#263123" },
    timeScale: { borderColor: "#263123", timeVisible: true },
  });
  charts.push(chart);
  return chart;
}

function lineOn(chart, data, color, opts = {}) {
  const series = chart.addLineSeries({ color, lineWidth: 2, ...opts });
  series.setData(data);
  return series;
}

function setLegend(elId, entries) {
  // entries: [{ color, label }, ...] - lightweight-charts has no built-in
  // legend, so this just renders colored dots next to labels above the
  // chart, in the same order/colors the series were plotted in.
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = entries
    .map((e) => `<span class="swatch"><span class="dot" style="background:${e.color}"></span>${e.label}</span>`)
    .join("");
}

function renderCards(detail) {
  const s = detail.latest_score;
  const sig = detail.latest_signals || {};
  const cardsEl = document.getElementById("dd-cards");
  if (!s) {
    cardsEl.innerHTML = `<div class="card"><h4>Status</h4><div class="value">Insufficient data</div></div>`;
    return;
  }
  const ah = sig.ah_line_shift?.detail_json || {};
  const x2 = sig.x2_displacement?.detail_json || {};
  const lim = sig.limit_movement?.detail_json || {};

  cardsEl.innerHTML = `
    <div class="card">
      <h4>Tier</h4>
      <div class="value"><span class="tier-badge tier-${s.tier}">${s.tier.replace("_", " ")}</span></div>
      <div class="sub">total score ${fmtNum(s.total_score, 2)} (AH ${fmtNum(s.ah_score)} × 1.4 + 1X2 ${fmtNum(s.x2_score)} + limit ${fmtNum(s.limit_bonus)} + convergence ${fmtNum(s.convergence_bonus)})</div>
    </div>
    <div class="card">
      <h4>Sharp side</h4>
      <div class="value">${s.sharp_side || "—"}</div>
      <div class="sub">${s.contested ? "Contested: early vs late-window direction disagree" : ""}</div>
    </div>
    <div class="card">
      <h4>AH line shift</h4>
      <div class="value">${ah.opening !== undefined ? `${fmtNum(ah.opening)} → ${fmtNum(ah.current)}` : "—"}</div>
      <div class="sub">shift ${fmtNum(ah.shift)} / direction ${ah.direction || "—"}</div>
    </div>
    <div class="card">
      <h4>1X2 displacement</h4>
      <div class="value">${fmtPp(x2.home_pp)} H / ${fmtPp(x2.draw_pp)} D / ${fmtPp(x2.away_pp)} A</div>
      <div class="sub">from true opening line</div>
    </div>
    <div class="card">
      <h4>Limit movement</h4>
      <div class="value">${fmtPct(lim.moneyline_limit_drop_pct)} 1X2 / ${fmtPct(lim.spread_limit_drop_pct)} AH</div>
      <div class="sub">% drop vs opening market limit</div>
    </div>
  `;
}

function renderRawTable(rows) {
  const body = document.getElementById("raw-table-body");
  body.innerHTML = rows
    .map(
      (r) => `
      <tr>
        <td>${new Date(r.captured_at).toLocaleString()}</td>
        <td class="dim">${r.market_key || ""}</td>
        <td>${r.market_type}</td>
        <td>${r.period}</td>
        <td>${r.is_alternate ? "alt" : "main"}</td>
        <td>${r.status || ""}</td>
        <td>${fmtNum(r.home_price)}</td>
        <td>${fmtNum(r.draw_price)}</td>
        <td>${fmtNum(r.away_price)}</td>
        <td>${fmtNum(r.home_points)}</td>
        <td>${fmtNum(r.limit_amount, 0)}</td>
        <td>${r.fair_home_prob != null ? fmtPct(r.fair_home_prob * 100) : "—"}</td>
        <td>${r.fair_draw_prob != null ? fmtPct(r.fair_draw_prob * 100) : "—"}</td>
        <td>${r.fair_away_prob != null ? fmtPct(r.fair_away_prob * 100) : "—"}</td>
      </tr>`
    )
    .join("");
}

const COLOR_HOME = "#39ff88";
const COLOR_DRAW = "#ffd60a";
const COLOR_AWAY = "#ff9500";
const COLOR_LINE = "#bf5af2";

function renderCharts(detail) {
  const warningEl = document.getElementById("chart-lib-warning");
  if (typeof LightweightCharts === "undefined") {
    warningEl.textContent =
      "Chart library failed to load from unpkg.com (CDN blocked or unreachable) - charts can't render, but the raw snapshot table below still has all the data.";
    warningEl.classList.remove("hidden");
    return;
  }
  warningEl.classList.add("hidden");
  destroyCharts();
  const ml = detail.series.moneyline_main;
  const sp = detail.series.spread_main;
  const tot = detail.series.total_main;
  const home = detail.matchup.home_team;
  const away = detail.matchup.away_team;

  const c1 = makeChart("chart-1x2-prob");
  lineOn(c1, toSeriesData(ml, "fair_home_prob").map((d) => ({ ...d, value: d.value * 100 })), COLOR_HOME);
  lineOn(c1, toSeriesData(ml, "fair_draw_prob").map((d) => ({ ...d, value: d.value * 100 })), COLOR_DRAW);
  lineOn(c1, toSeriesData(ml, "fair_away_prob").map((d) => ({ ...d, value: d.value * 100 })), COLOR_AWAY);
  setLegend("legend-1x2-prob", [
    { color: COLOR_HOME, label: `${home} (Home)` },
    { color: COLOR_DRAW, label: "Draw" },
    { color: COLOR_AWAY, label: `${away} (Away)` },
  ]);

  const c2 = makeChart("chart-1x2-odds");
  lineOn(c2, toSeriesData(ml, "home_price"), COLOR_HOME);
  lineOn(c2, toSeriesData(ml, "draw_price"), COLOR_DRAW);
  lineOn(c2, toSeriesData(ml, "away_price"), COLOR_AWAY);
  setLegend("legend-1x2-odds", [
    { color: COLOR_HOME, label: `${home} (Home)` },
    { color: COLOR_DRAW, label: "Draw" },
    { color: COLOR_AWAY, label: `${away} (Away)` },
  ]);

  const c3 = makeChart("chart-1x2-limit");
  lineOn(c3, toSeriesData(ml, "limit_amount"), COLOR_HOME);
  setLegend("legend-1x2-limit", [{ color: COLOR_HOME, label: "1X2 market limit ($)" }]);

  const c4 = makeChart("chart-ah-line");
  lineOn(c4, toSeriesData(sp, "home_points"), COLOR_LINE);
  setLegend("legend-ah-line", [{ color: COLOR_LINE, label: `${home} handicap (points)` }]);

  const c5 = makeChart("chart-ah-prob");
  lineOn(c5, toSeriesData(sp, "fair_home_prob").map((d) => ({ ...d, value: d.value * 100 })), COLOR_HOME);
  lineOn(c5, toSeriesData(sp, "fair_away_prob").map((d) => ({ ...d, value: d.value * 100 })), COLOR_AWAY);
  setLegend("legend-ah-prob", [
    { color: COLOR_HOME, label: `${home} (Home)` },
    { color: COLOR_AWAY, label: `${away} (Away)` },
  ]);

  const c6 = makeChart("chart-ah-limit");
  lineOn(c6, toSeriesData(sp, "limit_amount"), COLOR_LINE);
  setLegend("legend-ah-limit", [{ color: COLOR_LINE, label: "AH market limit ($)" }]);

  const c7 = makeChart("chart-total");
  lineOn(c7, toSeriesData(tot, "home_points"), COLOR_LINE, { priceScaleId: "left" });
  c7.priceScale("left").applyOptions({ visible: true });
  lineOn(c7, toSeriesData(tot, "home_price"), COLOR_HOME); // "home" slot = Over, see ingest.py normalize_market
  lineOn(c7, toSeriesData(tot, "away_price"), COLOR_AWAY); // "away" slot = Under
  setLegend("legend-total", [
    { color: COLOR_LINE, label: "Goal total line (left axis)" },
    { color: COLOR_HOME, label: "Over odds (right axis)" },
    { color: COLOR_AWAY, label: "Under odds (right axis)" },
  ]);

  const c8 = makeChart("chart-score");
  lineOn(c8, toSeriesData(detail.score_history, "total_score", "computed_at"), COLOR_HOME);
  setLegend("legend-score", [{ color: COLOR_HOME, label: "Composite score (0–10)" }]);
}

async function openDrilldown(matchupId) {
  currentDrilldownId = matchupId;
  const detail = await api(`/matches/${matchupId}/detail`);
  document.getElementById("dd-title").textContent = `${detail.matchup.home_team} vs ${detail.matchup.away_team}`;
  const ddCountdown = document.getElementById("dd-countdown");
  ddCountdown.dataset.start = detail.matchup.start_time;
  tickCountdowns();
  renderCards(detail);
  renderRawTable(detail.raw_snapshots);
  document.getElementById("drilldown").classList.remove("hidden");
  // charts need the container to be visible/sized before createChart
  requestAnimationFrame(() => renderCharts(detail));
}

document.getElementById("dd-close").onclick = () => {
  currentDrilldownId = null;
  destroyCharts();
  document.getElementById("drilldown").classList.add("hidden");
};
document.getElementById("dd-force-poll").onclick = async () => {
  if (currentDrilldownId) await api(`/poll/force/${currentDrilldownId}`, { method: "POST" });
};
document.getElementById("dd-unmonitor").onclick = async () => {
  if (!currentDrilldownId) return;
  await api(`/matches/${currentDrilldownId}?is_monitored=false`, { method: "PATCH" });
  document.getElementById("dd-close").click();
  await loadRounds(currentRound);
  loadSummary();
};

// ---------------------------------------------------------------------------
// Force poll all / websocket
// ---------------------------------------------------------------------------

document.getElementById("btn-force-all").onclick = async () => {
  const rows = await api("/matches/summary");
  await Promise.all(rows.map((r) => api(`/poll/force/${r.id}`, { method: "POST" })));
};

document.getElementById("btn-start-new-round").onclick = async () => {
  const rows = await api("/matches/summary"); // always the live set, regardless of viewed round
  if (!rows.length) return;
  if (!confirm(`Stop tracking all ${rows.length} currently live matches? This round stays available in the Round dropdown to review - use Discovery to add the new round.`)) {
    return;
  }
  const endedRound = currentRound;
  await Promise.all(rows.map((r) => api(`/matches/${r.id}?is_monitored=false`, { method: "PATCH" })));
  await loadRounds(endedRound); // keep viewing the round just ended, now shown as archived
  loadSummary();
};

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}${API}/ws`);
  const status = document.getElementById("ws-status");
  ws.onopen = () => {
    status.textContent = "live";
    status.className = "pill pill-ok";
  };
  ws.onclose = () => {
    status.textContent = "reconnecting…";
    status.className = "pill pill-err";
    setTimeout(connectWs, 3000);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "match_update" || msg.type === "error") {
      loadSummary();
      if (currentDrilldownId === msg.matchup_id) openDrilldown(msg.matchup_id);
    } else if (msg.type === "auto_unmonitored") {
      loadRounds(currentRound).then(loadSummary);
      if (currentDrilldownId === msg.matchup_id) {
        document.getElementById("dd-countdown").textContent = `Auto-removed from monitoring: ${msg.reason}`;
      }
    }
  };
}

async function init() {
  await loadRounds();
  await loadSummary();
  connectWs();
  setInterval(loadSummary, 30000);
  setInterval(tickCountdowns, 1000);
}
init();
