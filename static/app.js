const POSITION_ORDER = ["QB", "RB", "WR", "TE", "PICK"];

let MY_ROSTER_ID = null;
let MY_TEAM_NAME = "";
let MY_TEAM_INFO = null;      // {tier, wins, losses, record_used}
let OPPONENT_TEAM_INFO = null; // {team, tier, wins, losses, record_used}
let OPPONENT_ROSTER_ID = null;
let MY_ASSETS = [];    // full roster, refreshed once
let THEIR_ASSETS = []; // full roster, refreshed on opponent change
let REPORT_LOADED = false;
let FA_LOADED = false;
let FA_DATA = [];
let FA_POS_FILTER = "ALL";

init();
initTabs();

function initTabs() {
  const buttons = document.querySelectorAll(".tab-btn");
  buttons.forEach(btn => {
    btn.addEventListener("click", () => {
      buttons.forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(btn.dataset.tab).classList.add("active");
      document.getElementById("trade-tab-controls").style.display = btn.dataset.tab === "trade-tab" ? "flex" : "none";
      if (btn.dataset.tab === "report-tab" && !REPORT_LOADED) {
        REPORT_LOADED = true;
        loadReport();
      }
      if (btn.dataset.tab === "fa-tab" && !FA_LOADED) {
        FA_LOADED = true;
        loadFreeAgents();
      }
    });
  });
}

async function loadFreeAgents() {
  let data;
  try {
    data = await fetchJSON("/api/free_agents");
  } catch (e) {
    document.getElementById("fa-table-container").innerHTML = `<p class="error">${e.message}</p>`;
    return;
  }
  FA_DATA = data.free_agents;
  renderFaSuggested();
  document.getElementById("fa-search").addEventListener("input", renderFaTable);
  document.querySelectorAll(".pos-filter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".pos-filter-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      FA_POS_FILTER = btn.dataset.pos;
      renderFaTable();
    });
  });
  renderFaTable();
}

function compareCell(a) {
  const c = a.roster_comparison;
  if (!c) return "-";
  const sign = c.value_diff >= 0 ? "+" : "";
  return c.worth_drop
    ? `Upgrade over ${c.player_name} (${sign}${c.value_diff.toLocaleString()})`
    : `Below ${c.player_name} (${sign}${c.value_diff.toLocaleString()})`;
}

function renderFaSuggested() {
  const container = document.getElementById("fa-suggested");
  const topValue = FA_DATA.slice(0, 5);
  const trending = FA_DATA
    .filter(a => a.trend_delta_pct !== null && a.trend_delta_pct >= 15)
    .sort((a, b) => b.trend_delta_pct - a.trend_delta_pct)
    .slice(0, 5);
  const upgrades = FA_DATA
    .filter(a => a.roster_comparison && a.roster_comparison.worth_drop)
    .sort((a, b) => b.roster_comparison.value_diff - a.roster_comparison.value_diff)
    .slice(0, 8);

  container.innerHTML = "";
  const sec = document.createElement("div");
  sec.className = "report-section";
  const h3 = document.createElement("h3");
  h3.textContent = "Suggested Pickups";
  sec.appendChild(h3);

  if (upgrades.length) {
    const h4 = document.createElement("h4");
    h4.textContent = "Worth dropping someone for -- beats your weakest player at that position";
    sec.appendChild(h4);
    sec.appendChild(buildTable(
      ["Player", "Pos", "Team", "Age", "Value", "vs Your Roster"],
      upgrades.map(a => [a.full_name, a.position, a.team || "-", a.age ?? "-", a.value.toLocaleString(), compareCell(a)])
    ));
  }
  if (topValue.length) {
    const h4 = document.createElement("h4");
    h4.textContent = "Highest value available";
    sec.appendChild(h4);
    sec.appendChild(buildTable(
      ["Player", "Pos", "Team", "Age", "Value", "vs Your Roster"],
      topValue.map(a => [a.full_name, a.position, a.team || "-", a.age ?? "-", a.value.toLocaleString(), compareCell(a)])
    ));
  }
  if (trending.length) {
    const h4 = document.createElement("h4");
    h4.textContent = "Trending up 15%+ this week -- grab before your league notices";
    sec.appendChild(h4);
    sec.appendChild(buildTable(
      ["Player", "Pos", "Team", "Age", "Value", "7d Change", "vs Your Roster"],
      trending.map(a => [a.full_name, a.position, a.team || "-", a.age ?? "-", a.value.toLocaleString(), `+${a.trend_delta_pct.toFixed(1)}%`, compareCell(a)])
    ));
  }
  container.appendChild(sec);
}

function renderFaTable() {
  const search = document.getElementById("fa-search").value.trim().toLowerCase();
  const filtered = FA_DATA.filter(a => {
    if (FA_POS_FILTER !== "ALL" && a.position !== FA_POS_FILTER) return false;
    if (search && !a.full_name.toLowerCase().includes(search)) return false;
    return true;
  });
  const rows = filtered.map(a => [
    a.full_name, a.position, a.team || "-", a.age ?? "-", a.value.toLocaleString(),
    a.trend_delta_pct !== null ? `${a.trend_delta_pct >= 0 ? "+" : ""}${a.trend_delta_pct.toFixed(1)}%` : "-",
    compareCell(a),
  ]);
  const container = document.getElementById("fa-table-container");
  container.innerHTML = "";
  container.appendChild(buildTable(["Player", "Pos", "Team", "Age", "Value", "7d Change", "vs Your Roster"], rows));
}

async function loadReport() {
  const container = document.getElementById("report-content");
  let data;
  try {
    data = await fetchJSON("/api/report");
  } catch (e) {
    container.innerHTML = `<p class="error">${e.message}</p>`;
    return;
  }
  container.innerHTML = "";
  container.appendChild(reportTotalValue(data.total_value));
  container.appendChild(reportPositionValue(data.position_value));
  container.appendChild(reportStarterVsBench(data.starter_vs_bench));
  container.appendChild(reportAge(data.age));
  container.appendChild(reportPickCapital(data.pick_capital));
  container.appendChild(reportValueMovers(data.value_movers));
  container.appendChild(reportStrategy(data.strategy));
}

function reportSection(title, ...children) {
  const sec = document.createElement("div");
  sec.className = "report-section";
  const h3 = document.createElement("h3");
  h3.textContent = title;
  sec.appendChild(h3);
  children.forEach(c => sec.appendChild(c));
  return sec;
}

function buildTable(headers, rows) {
  const table = document.createElement("table");
  table.className = "report-table";
  const thead = document.createElement("tr");
  headers.forEach(h => {
    const th = document.createElement("th");
    th.textContent = h;
    thead.appendChild(th);
  });
  table.appendChild(thead);
  rows.forEach(cells => {
    const tr = document.createElement("tr");
    cells.forEach(c => {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    });
    table.appendChild(tr);
  });
  return table;
}

function reportTotalValue(rows) {
  const table = buildTable(
    ["Rank", "Team", "Total Value"],
    rows.map((r, i) => [i + 1, r.team, r.total_value.toLocaleString()])
  );
  return reportSection("1. Total Roster Value Ranking", table);
}

function reportPositionValue(data) {
  const medians = document.createElement("p");
  medians.className = "hint";
  medians.textContent = "League medians: " + POSITION_ORDER.slice(0, 4)
    .map(pos => `${pos}: ${Math.round(data.medians[pos]).toLocaleString()}`).join("  ");
  const table = buildTable(
    ["Team", "QB (vs med)", "RB (vs med)", "WR (vs med)", "TE (vs med)"],
    data.teams.map(t => [
      t.team,
      ...POSITION_ORDER.slice(0, 4).map(pos => {
        const p = t.positions[pos];
        const sign = p.diff >= 0 ? "+" : "";
        return `${p.value.toLocaleString()} (${sign}${Math.round(p.diff).toLocaleString()})`;
      }),
    ])
  );
  return reportSection("2. Position Value vs. League Median", medians, table);
}

function reportStarterVsBench(rows) {
  const table = buildTable(
    ["Team", "Starter Value", "Bench Value", "Bench %"],
    rows.map(r => [r.team, r.starter_value.toLocaleString(), r.bench_value.toLocaleString(), `${r.bench_pct.toFixed(0)}%`])
  );
  return reportSection("3. Starter vs. Bench Value", table);
}

function reportAge(rows) {
  const table = buildTable(
    ["Team", "Wtd Avg Age"],
    rows.map(r => [r.team, r.wtd_age !== null ? r.wtd_age.toFixed(1) : "N/A"])
  );
  return reportSection("4. Value-Weighted Average Age", table);
}

function reportPickCapital(data) {
  const table = buildTable(
    ["Team", "Pick Value", "# Picks", "Received Picks"],
    data.teams.map(r => [
      r.team,
      r.pick_value.toLocaleString(),
      r.num_picks,
      r.received.length ? r.received.map(p => `${p.season} Rd${p.round} (${p.from_team})`).join(", ") : "-",
    ])
  );
  const children = [table];
  if (data.picks_unparsed) {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = "FantasyCalc pick entries could not be parsed -- pick values shown as 0. Pick counts still reflect traded pick ownership.";
    children.push(note);
  }
  return reportSection("5. Draft Pick Capital", ...children);
}

function reportValueMovers(data) {
  if (!data) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "No historical data yet -- run ingest.py daily for at least 2 days to see trends.";
    return reportSection("6. Value Movers (7-Day Trend)", p);
  }
  const children = [];
  if (data.risers.length) {
    const h4 = document.createElement("h4");
    h4.textContent = "Top Risers -- consider SELLING HIGH";
    children.push(h4, buildTable(
      ["Player", "Pos", "7d Ago", "Now", "Change"],
      data.risers.map(d => [d.name, d.position, d.prev.toLocaleString(), d.current.toLocaleString(), `+${d.delta_pct.toFixed(1)}%`])
    ));
  }
  if (data.fallers.length) {
    const h4 = document.createElement("h4");
    h4.textContent = "Top Fallers -- consider BUYING LOW";
    children.push(h4, buildTable(
      ["Player", "Pos", "7d Ago", "Now", "Change"],
      data.fallers.map(d => [d.name, d.position, d.prev.toLocaleString(), d.current.toLocaleString(), `${d.delta_pct.toFixed(1)}%`])
    ));
  }
  return reportSection("6. Value Movers (7-Day Trend)", ...children);
}

function reportStrategy(data) {
  const table = buildTable(
    ["Team", "Tier", "Score", "Record", "Starter Val", "Total Val", "Pick Capital"],
    data.teams.map(r => [
      r.team, r.tier, `${(r.score * 100).toFixed(0)}%`, r.record || "-",
      r.starter_value.toLocaleString(), r.total_value.toLocaleString(), r.pick_capital.toLocaleString(),
    ])
  );
  const basis = document.createElement("p");
  basis.className = "hint";
  basis.textContent = data.use_record
    ? "Tier basis: 40% starter rank + 20% total rank + 40% win rate"
    : "Tier basis: 60% starter rank + 40% total rank (offseason -- no record)";
  const legend = document.createElement("p");
  legend.className = "hint";
  legend.textContent = "Contending (>=60%) = compete now  |  Middle = flexible  |  Rebuilding (<=35%) = accumulate picks";
  return reportSection("7. Strategy Assessment", table, basis, legend);
}

async function init() {
  const teamsData = await fetchJSON("/api/teams");
  MY_ROSTER_ID = teamsData.my_roster_id;
  MY_TEAM_NAME = teamsData.my_team;
  MY_TEAM_INFO = teamsData.my_tier;
  document.getElementById("my-team-name").textContent = `My Roster (${MY_TEAM_NAME})`;

  const select = document.getElementById("opponent-select");
  select.innerHTML = "";
  for (const t of teamsData.teams) {
    const opt = document.createElement("option");
    opt.value = t.roster_id;
    const record = t.record_used ? ` ${t.wins}-${t.losses}` : "";
    opt.textContent = `${t.team} [${t.tier}${record}]`;
    select.appendChild(opt);
  }
  select.addEventListener("change", () => loadOpponent(parseInt(select.value, 10)));

  MY_ASSETS = (await fetchJSON(`/api/roster/${MY_ROSTER_ID}`)).assets;
  renderRosterList("my-roster-list", MY_ASSETS);

  if (teamsData.teams.length) {
    OPPONENT_ROSTER_ID = teamsData.teams[0].roster_id;
    select.value = OPPONENT_ROSTER_ID;
    await loadOpponent(OPPONENT_ROSTER_ID);
  }

  document.getElementById("find-trades-btn").addEventListener("click", findTrades);
}

async function loadOpponent(rosterId) {
  OPPONENT_ROSTER_ID = rosterId;
  const data = await fetchJSON(`/api/roster/${rosterId}`);
  THEIR_ASSETS = data.assets;
  OPPONENT_TEAM_INFO = { team: data.team, tier: data.tier, wins: data.wins, losses: data.losses, record_used: data.record_used };
  document.getElementById("their-team-name").textContent = `Their Roster (${data.team})`;
  renderRosterList("their-roster-list", THEIR_ASSETS);
  document.getElementById("packages").innerHTML = "";
}

function renderRosterList(containerId, assets) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  const byPos = groupByPosition(assets);
  for (const pos of POSITION_ORDER) {
    const group = byPos[pos];
    if (!group || !group.length) continue;
    const h3 = document.createElement("h3");
    h3.textContent = pos;
    container.appendChild(h3);
    for (const a of group) {
      container.appendChild(assetRow(a, "seed"));
    }
  }
}

function assetRow(a, name) {
  const row = document.createElement("label");
  row.className = "asset-row" + (a.untouchable ? " untouchable" : "");
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.name = name;
  cb.value = a.player_id;
  row.appendChild(cb);
  const label = document.createElement("span");
  const ageStr = a.age ? `, age ${Math.round(a.age)}` : "";
  label.textContent = `${a.full_name} (${a.position}${ageStr}) — ${a.value.toLocaleString()}`;
  row.appendChild(label);
  if (a.untouchable) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = "untouchable";
    row.appendChild(tag);
  }
  return row;
}

function groupByPosition(assets) {
  const out = {};
  for (const a of assets) {
    (out[a.position] = out[a.position] || []).push(a);
  }
  for (const pos in out) {
    out[pos].sort((x, y) => y.value - x.value);
  }
  return out;
}

function getCheckedIds(containerId) {
  return Array.from(document.querySelectorAll(`#${containerId} input[type=checkbox]:checked`)).map(cb => cb.value);
}

async function findTrades() {
  const seedSendIds = getCheckedIds("my-roster-list");
  const seedRecvIds = getCheckedIds("their-roster-list");

  const container = document.getElementById("packages");
  container.innerHTML = "<p class='loading'>Finding trades...</p>";

  let data;
  try {
    data = await postJSON("/api/find_trades", {
      opponent_roster_id: OPPONENT_ROSTER_ID,
      seed_send_ids: seedSendIds,
      seed_recv_ids: seedRecvIds,
    });
  } catch (e) {
    container.innerHTML = `<p class="error">${e.message}</p>`;
    return;
  }

  container.innerHTML = "";
  if (data.pick_note) {
    const note = document.createElement("p");
    note.className = "pick-note";
    note.textContent = data.pick_note;
    container.appendChild(note);
  }
  if (!data.packages.length) {
    container.innerHTML += "<p>No fair packages found for these seeds. Try different players or fewer constraints.</p>";
    return;
  }
  data.packages.forEach((pkg, i) => container.appendChild(buildPackageCard(pkg, i)));
}

function buildPackageCard(pkg, index) {
  const card = document.createElement("div");
  card.className = "package-card";
  card.dataset.index = index;

  const sendIds = new Set(pkg.send.assets.map(a => a.player_id));
  const recvIds = new Set(pkg.recv.assets.map(a => a.player_id));

  card.innerHTML = `
    <div class="card-header">
      <h3>Package ${index + 1}</h3>
      <div class="card-header-actions">
        <span class="fair-badge"></span>
        <button class="copy-ai-btn" type="button">Copy for AI</button>
      </div>
    </div>
    <div class="card-cols">
      <div class="card-col" data-side="send">
        <h4>You Send <span class="total"></span></h4>
        <div class="chips"></div>
        <select class="add-select">
          <option value="">+ Add from your roster</option>
        </select>
      </div>
      <div class="card-col" data-side="recv">
        <h4>You Get <span class="total"></span></h4>
        <div class="chips"></div>
        <select class="add-select">
          <option value="">+ Add from their roster</option>
        </select>
      </div>
    </div>
    <div class="card-details"></div>
  `;

  populateAddSelect(card.querySelector('[data-side="send"] .add-select'), MY_ASSETS, sendIds);
  populateAddSelect(card.querySelector('[data-side="recv"] .add-select'), THEIR_ASSETS, recvIds);

  card.querySelector('[data-side="send"] .add-select').addEventListener("change", (e) => {
    if (e.target.value) { sendIds.add(e.target.value); refreshCard(card, sendIds, recvIds); }
  });
  card.querySelector('[data-side="recv"] .add-select').addEventListener("change", (e) => {
    if (e.target.value) { recvIds.add(e.target.value); refreshCard(card, sendIds, recvIds); }
  });

  card.querySelector(".copy-ai-btn").addEventListener("click", (e) => copyForAi(card, e.target));

  renderCardBody(card, pkg, sendIds, recvIds);
  return card;
}

function populateAddSelect(select, allAssets, excludeIds) {
  select.innerHTML = '<option value="">+ Add asset</option>';
  const byPos = groupByPosition(allAssets.filter(a => !excludeIds.has(a.player_id)));
  for (const pos of POSITION_ORDER) {
    const group = byPos[pos];
    if (!group || !group.length) continue;
    for (const a of group) {
      const opt = document.createElement("option");
      opt.value = a.player_id;
      opt.textContent = `${a.full_name} (${a.position}) — ${a.value.toLocaleString()}`;
      select.appendChild(opt);
    }
  }
}

async function refreshCard(card, sendIds, recvIds) {
  let view;
  try {
    view = await postJSON("/api/evaluate", {
      send_roster_id: MY_ROSTER_ID,
      send_ids: Array.from(sendIds),
      recv_roster_id: OPPONENT_ROSTER_ID,
      recv_ids: Array.from(recvIds),
    });
  } catch (e) {
    card.querySelector(".card-details").innerHTML = `<p class="error">${e.message}</p>`;
    return;
  }
  renderCardBody(card, view, sendIds, recvIds);
}

function renderCardBody(card, view, sendIds, recvIds) {
  card._lastView = view;
  const badge = card.querySelector(".fair-badge");
  badge.textContent = view.fair ? "Fair" : "Not Fair";
  badge.className = "fair-badge " + (view.fair ? "fair" : "unfair");

  renderSide(card, "send", view.send, sendIds, recvIds);
  renderSide(card, "recv", view.recv, recvIds, sendIds);

  const details = card.querySelector(".card-details");
  details.innerHTML = "";

  if (view.dynasty_warning) {
    const d = document.createElement("p");
    d.className = "dynasty-warning";
    d.textContent = view.dynasty_warning;
    details.appendChild(d);
  }

  const trendBits = [];
  if (view.trend_signals.send.length) trendBits.push("Sell high: " + view.trend_signals.send.join(", "));
  if (view.trend_signals.recv.length) trendBits.push("Buy low: " + view.trend_signals.recv.join(", "));
  if (trendBits.length) {
    const t = document.createElement("p");
    t.className = "trend-signals";
    t.textContent = trendBits.join(" | ");
    details.appendChild(t);
  }

  const notes = [...view.annotations.my_side, ...view.annotations.their_side];
  if (notes.length) {
    const ul = document.createElement("ul");
    ul.className = "annotations";
    for (const n of notes) {
      const li = document.createElement("li");
      li.textContent = n;
      ul.appendChild(li);
    }
    details.appendChild(ul);
  }

  if (view.surplus_impact) {
    details.appendChild(renderSurplusImpact(view.surplus_impact));
  }
}

function renderSide(card, side, sideView, ids, otherIds) {
  const col = card.querySelector(`[data-side="${side}"]`);
  col.querySelector(".total").textContent = `(${sideView.total_value.toLocaleString()})`;
  const chips = col.querySelector(".chips");
  chips.innerHTML = "";
  for (const a of sideView.assets) {
    const chip = document.createElement("span");
    chip.className = "chip" + (a.untouchable ? " untouchable" : "");
    chip.textContent = `${a.full_name} (${a.value.toLocaleString()})`;
    const remove = document.createElement("button");
    remove.textContent = "×";
    remove.title = "Remove";
    remove.addEventListener("click", () => {
      ids.delete(a.player_id);
      refreshCard(card, side === "send" ? ids : otherIds, side === "send" ? otherIds : ids);
    });
    chip.appendChild(remove);
    chips.appendChild(chip);
  }
  const allAssets = side === "send" ? MY_ASSETS : THEIR_ASSETS;
  populateAddSelect(col.querySelector(".add-select"), allAssets, ids);
}

function renderSurplusImpact(impact) {
  const wrap = document.createElement("div");
  wrap.className = "surplus-impact";
  wrap.appendChild(surplusTable("Your positional surplus", impact.my));
  wrap.appendChild(surplusTable("Their positional surplus", impact.their));
  return wrap;
}

function surplusTable(title, beforeAfter) {
  const wrap = document.createElement("div");
  const h5 = document.createElement("h5");
  h5.textContent = title;
  wrap.appendChild(h5);
  const table = document.createElement("table");
  const positions = Object.keys(beforeAfter.before);
  const headRow = document.createElement("tr");
  headRow.innerHTML = "<th>Pos</th><th>Before</th><th>After</th>";
  table.appendChild(headRow);
  for (const pos of positions) {
    const tr = document.createElement("tr");
    const before = beforeAfter.before[pos];
    const after = beforeAfter.after[pos];
    const delta = after - before;
    const deltaClass = delta > 0 ? "up" : delta < 0 ? "down" : "";
    tr.innerHTML = `<td>${pos}</td><td>${fmtSigned(before)}</td><td class="${deltaClass}">${fmtSigned(after)}</td>`;
    table.appendChild(tr);
  }
  wrap.appendChild(table);
  return wrap;
}

function fmtSigned(n) {
  const rounded = Math.round(n);
  return (rounded >= 0 ? "+" : "") + rounded.toLocaleString();
}

function teamStatusLine(info) {
  if (!info) return "unknown";
  const record = info.record_used ? `, ${info.wins}-${info.losses}` : "";
  return `${info.tier}${record}`;
}

function buildTradeSummaryText(view) {
  const lines = [];
  lines.push("I'm evaluating a dynasty fantasy football trade (Superflex, 1.0 PPR, TE premium). Here's the full picture:");
  lines.push("");
  lines.push(`My team: ${view.send.team} [${teamStatusLine(MY_TEAM_INFO)}]`);
  lines.push(`Their team: ${view.recv.team} [${teamStatusLine(OPPONENT_TEAM_INFO)}]`);
  lines.push("");
  lines.push("The trade:");
  lines.push("");
  lines.push(`${view.send.team} sends:`);
  for (const a of view.send.assets) lines.push(`- ${a.full_name} (${a.position}, ${a.value.toLocaleString()})`);
  lines.push(`Total: ${view.send.total_value.toLocaleString()}`);
  lines.push("");
  lines.push(`${view.recv.team} sends:`);
  for (const a of view.recv.assets) lines.push(`- ${a.full_name} (${a.position}, ${a.value.toLocaleString()})`);
  lines.push(`Total: ${view.recv.total_value.toLocaleString()}`);
  lines.push("");
  lines.push(`My tool's verdict: ${view.fair ? "Fair" : "Not fair"} (value ratio ${view.value_ratio ? view.value_ratio.toFixed(2) : "n/a"}, tolerance +/-${Math.round(view.tolerance * 100)}%)`);
  if (view.dynasty_warning) lines.push(`Dynasty note: ${view.dynasty_warning}`);

  const trendBits = [];
  if (view.trend_signals.send.length) trendBits.push("Sell high: " + view.trend_signals.send.join(", "));
  if (view.trend_signals.recv.length) trendBits.push("Buy low: " + view.trend_signals.recv.join(", "));
  if (trendBits.length) lines.push(`This week's value trends: ${trendBits.join(" | ")}`);

  const notes = [...(view.annotations?.my_side || []), ...(view.annotations?.their_side || [])];
  if (notes.length) lines.push(`Notes: ${notes.join("; ")}`);

  lines.push("");
  lines.push("For full context, here's my entire current roster:");
  const byPos = groupByPosition(MY_ASSETS);
  for (const pos of POSITION_ORDER) {
    const group = byPos[pos];
    if (!group || !group.length) continue;
    const label = pos === "PICK" ? "Picks" : pos;
    lines.push(`${label}: ` + group.map(a => `${a.full_name} (${a.value.toLocaleString()})`).join(", "));
  }

  lines.push("");
  lines.push("What do you think -- is this fair, and does it make sense for my team long-term given my roster and situation?");
  return lines.join("\n");
}

async function copyForAi(card, button) {
  const view = card._lastView;
  if (!view) return;
  const text = buildTradeSummaryText(view);
  const originalLabel = button.textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "Copied!";
  } catch (e) {
    // Clipboard API unavailable (e.g. insecure context) -- fall back to a selectable textarea
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.className = "copy-fallback";
    card.appendChild(ta);
    ta.select();
    button.textContent = "Select & copy below";
  }
  setTimeout(() => { button.textContent = originalLabel; }, 2000);
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error((await res.json()).error || res.statusText);
  return res.json();
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json()).error || res.statusText);
  return res.json();
}
