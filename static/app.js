const POSITION_ORDER = ["QB", "RB", "WR", "TE", "PICK"];

let MY_ROSTER_ID = null;
let MY_TEAM_NAME = "";
let OPPONENT_ROSTER_ID = null;
let MY_ASSETS = [];    // full roster, refreshed once
let THEIR_ASSETS = []; // full roster, refreshed on opponent change

init();

async function init() {
  const teamsData = await fetchJSON("/api/teams");
  MY_ROSTER_ID = teamsData.my_roster_id;
  MY_TEAM_NAME = teamsData.my_team;
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
      <span class="fair-badge"></span>
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
