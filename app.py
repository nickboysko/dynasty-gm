"""
Interactive trade builder web app.

Run with: python app.py
Serves a local-only (127.0.0.1) UI for building trades: pick a partner team,
seed specific players/picks you want in the deal (from either roster), get
generated packages built around those seeds, then edit any package live.

Data is loaded once at startup from dynasty.db. Since the DB only changes via
the daily 7am ingest, restart this app to pick up fresh data -- no polling.
"""

from flask import Flask, abort, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import copy

import db
from utils import (
    POSITIONS,
    UNTOUCHABLES,
    build_pick_value_table,
    classify_teams,
    compute_pick_assets,
    filter_untouchables,
    get_free_agents,
    get_latest_fc_values,
    get_rosters,
    get_value_trends,
    load_settings,
)
from trade_finder import (
    CONSOLIDATION_PREMIUM,
    MIN_ASSET_VALUE,
    MY_TEAM,
    PICK_PREMIUM_REBUILD,
    PLAYER_DISCOUNT_REBUILD,
    TOLERANCE,
    _dynasty_warning,
    _is_fair,
    _pos_annotations,
    _rebuild_adjusted_assets,
    _trend_signals,
    compute_positional_surplus,
    find_my_roster,
    generate_packages_seeded,
    rank_trade_partners,
)
from report import (
    compute_age,
    compute_pick_capital,
    compute_position_value,
    compute_starter_vs_bench,
    compute_strategy,
    compute_total_value,
    compute_value_movers,
)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Startup: load league data once, hold it in memory for the process lifetime
# ---------------------------------------------------------------------------

class State:
    pass


STATE = State()


def load_state():
    conn = db.get_connection()
    STATE.settings = load_settings(conn)
    STATE.fc_values = get_latest_fc_values(conn)
    STATE.rosters = get_rosters(conn, STATE.fc_values)
    pick_values = build_pick_value_table(STATE.fc_values)
    STATE.pick_assets = compute_pick_assets(conn, STATE.rosters, STATE.settings["draft_rounds"], pick_values)
    STATE.surplus = compute_positional_surplus(STATE.rosters, STATE.settings["starting_slots"])
    STATE.tiers = classify_teams(STATE.rosters, STATE.settings["starting_slots"])
    STATE.trends = get_value_trends(conn, days=7)
    STATE.my_roster = find_my_roster(STATE.rosters, MY_TEAM)
    STATE.roster_by_id = {r["roster_id"]: r for r in STATE.rosters}
    STATE.traded_picks_rows = conn.execute(
        "SELECT season, round, original_roster_id, current_roster_id FROM traded_picks"
    ).fetchall()
    STATE.free_agents = get_free_agents(conn, STATE.fc_values)
    conn.close()


db.init_db()
load_state()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

@app.errorhandler(HTTPException)
def handle_http_exception(e):
    return jsonify({"error": e.description}), e.code


def get_roster(roster_id):
    roster = STATE.roster_by_id.get(roster_id)
    if roster is None:
        abort(404, description=f"Unknown roster_id {roster_id}")
    return roster


def get_sendable_assets(roster_id):
    """All players + picks for a roster, as a flat list of asset dicts. Unfiltered
    by value/position -- the picker shows everything for full manual control."""
    roster = get_roster(roster_id)
    return roster["players"] + STATE.pick_assets.get(roster_id, [])


def resolve_ids(roster_id, ids):
    assets_by_id = {a["player_id"]: a for a in get_sendable_assets(roster_id)}
    resolved, missing = [], []
    for pid in ids:
        if pid in assets_by_id:
            resolved.append(assets_by_id[pid])
        else:
            missing.append(pid)
    if missing:
        abort(400, description=f"Unknown asset ids for roster {roster_id}: {missing}")
    return resolved


def asset_json(a):
    return {
        "player_id": a["player_id"],
        "full_name": a["full_name"],
        "position": a["position"],
        "team": a.get("team"),
        "age": a.get("age"),
        "value": a["value"],
        "untouchable": a.get("full_name", "").lower() in UNTOUCHABLES,
        "kind": "pick" if a["position"] == "PICK" else "player",
    }


def build_trade_view(send_rid, send_assets, recv_rid, recv_assets):
    sv = sum(a["value"] for a in send_assets)
    rv = sum(a["value"] for a in recv_assets)
    return {
        "send": {
            "roster_id": send_rid,
            "team": STATE.roster_by_id[send_rid]["team"],
            "assets": [asset_json(a) for a in send_assets],
            "total_value": sv,
        },
        "recv": {
            "roster_id": recv_rid,
            "team": STATE.roster_by_id[recv_rid]["team"],
            "assets": [asset_json(a) for a in recv_assets],
            "total_value": rv,
        },
        "fair": _is_fair(send_assets, recv_assets) if send_assets and recv_assets else False,
        "value_ratio": (sv / rv) if rv else None,
        "tolerance": TOLERANCE,
        "consolidation_premium": CONSOLIDATION_PREMIUM,
        "dynasty_warning": _dynasty_warning(send_assets, recv_assets),
        "trend_signals": {
            "send": _trend_signals(send_assets, STATE.trends),
            "recv": _trend_signals(recv_assets, STATE.trends),
        },
        "annotations": {
            "my_side": _pos_annotations(STATE.surplus[send_rid], send_assets, recv_assets),
            "their_side": _pos_annotations(STATE.surplus[recv_rid], recv_assets, send_assets),
        },
    }


def compute_surplus_impact(send_rid, send_assets, recv_rid, recv_assets):
    """Positional surplus before/after this exact trade, for both rosters.
    Assumes acquired players are immediately starter-eligible (ignores taxi/IR) --
    fine for a live preview."""
    rosters_copy = copy.deepcopy(STATE.rosters)
    by_id = {r["roster_id"]: r for r in rosters_copy}

    send_ids = {a["player_id"] for a in send_assets if a["position"] != "PICK"}
    recv_ids = {a["player_id"] for a in recv_assets if a["position"] != "PICK"}

    send_roster = by_id[send_rid]
    recv_roster = by_id[recv_rid]

    moved_from_send = [p for p in send_roster["players"] if p["player_id"] in send_ids]
    moved_from_recv = [p for p in recv_roster["players"] if p["player_id"] in recv_ids]
    for p in moved_from_send + moved_from_recv:
        p["slot"] = "active"

    send_roster["players"] = [p for p in send_roster["players"] if p["player_id"] not in send_ids] + moved_from_recv
    recv_roster["players"] = [p for p in recv_roster["players"] if p["player_id"] not in recv_ids] + moved_from_send

    surplus_after = compute_positional_surplus(rosters_copy, STATE.settings["starting_slots"])

    return {
        "my": {"before": STATE.surplus[send_rid], "after": surplus_after[send_rid]},
        "their": {"before": STATE.surplus[recv_rid], "after": surplus_after[recv_rid]},
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/teams")
def api_teams():
    partners = rank_trade_partners(STATE.my_roster, STATE.rosters, STATE.surplus)
    teams = []
    for score, r in partners:
        rid = r["roster_id"]
        info = STATE.tiers[rid]
        teams.append({
            "roster_id": rid,
            "team": r["team"],
            "tier": info["tier"],
            "wins": info["wins"],
            "losses": info["losses"],
            "record_used": info["record_used"],
            "fit_score": score,
        })
    return jsonify({
        "my_roster_id": STATE.my_roster["roster_id"],
        "my_team": STATE.my_roster["team"],
        "teams": teams,
    })


@app.route("/api/roster/<int:roster_id>")
def api_roster(roster_id):
    roster = get_roster(roster_id)
    assets = get_sendable_assets(roster_id)
    return jsonify({
        "roster_id": roster_id,
        "team": roster["team"],
        "tier": STATE.tiers[roster_id]["tier"],
        "assets": [asset_json(a) for a in assets],
    })


@app.route("/api/report")
def api_report():
    slots = STATE.settings["starting_slots"]
    strategy = compute_strategy(STATE.rosters, slots, STATE.pick_assets)
    return jsonify({
        "total_value": compute_total_value(STATE.rosters),
        "position_value": compute_position_value(STATE.rosters),
        "starter_vs_bench": compute_starter_vs_bench(STATE.rosters, slots),
        "age": compute_age(STATE.rosters),
        "pick_capital": compute_pick_capital(
            STATE.rosters, STATE.fc_values, STATE.settings["draft_rounds"], STATE.traded_picks_rows
        ),
        "value_movers": compute_value_movers(STATE.trends),
        "strategy": strategy,
    })


@app.route("/api/free_agents")
def api_free_agents():
    agents = [a for a in STATE.free_agents if a["position"] in POSITIONS and a["value"] > 0]
    agents.sort(key=lambda a: a["value"], reverse=True)
    result = []
    for a in agents:
        trend = STATE.trends.get(a["player_id"])
        result.append({**a, "trend_delta_pct": trend["delta_pct"] if trend else None})
    return jsonify({"free_agents": result})


@app.route("/api/find_trades", methods=["POST"])
def api_find_trades():
    data = request.get_json(force=True) or {}
    if data.get("opponent_roster_id") is None:
        abort(400, description="opponent_roster_id is required")
    opponent_rid = int(data["opponent_roster_id"])
    my_rid = STATE.my_roster["roster_id"]
    if opponent_rid == my_rid:
        abort(400, description="opponent_roster_id cannot be your own roster")
    opponent_roster = get_roster(opponent_rid)

    seed_send = resolve_ids(my_rid, data.get("seed_send_ids") or [])
    seed_recv = resolve_ids(opponent_rid, data.get("seed_recv_ids") or [])
    seed_send_ids = {a["player_id"] for a in seed_send}
    seed_recv_ids = {a["player_id"] for a in seed_recv}

    my_pool = filter_untouchables([
        p for p in STATE.my_roster["players"]
        if p["position"] in POSITIONS and p["value"] >= MIN_ASSET_VALUE and p["player_id"] not in seed_send_ids
    ]) + [
        p for p in STATE.pick_assets.get(my_rid, [])
        if p["value"] >= MIN_ASSET_VALUE and p["player_id"] not in seed_send_ids
    ]
    their_players = [
        p for p in opponent_roster["players"]
        if p["position"] in POSITIONS and p["value"] >= MIN_ASSET_VALUE and p["player_id"] not in seed_recv_ids
    ]
    their_picks = [
        p for p in STATE.pick_assets.get(opponent_rid, [])
        if p["value"] >= MIN_ASSET_VALUE and p["player_id"] not in seed_recv_ids
    ]

    opponent_tier = STATE.tiers[opponent_rid]["tier"]
    restore_map = {}
    pick_note = None

    if opponent_tier == "Rebuilding":
        my_picks_pool = [a for a in my_pool if a["position"] == "PICK"]
        my_players_pool = [a for a in my_pool if a["position"] != "PICK"]
        seed_recv_players = [a for a in seed_recv if a["position"] != "PICK"]
        seed_recv_picks = [a for a in seed_recv if a["position"] == "PICK"]

        adj_my_picks, adj_their_players = _rebuild_adjusted_assets(my_picks_pool, their_players)
        _, adj_seed_recv_players = _rebuild_adjusted_assets([], seed_recv_players)

        for a in adj_my_picks:
            restore_map[a["player_id"]] = 1 + PICK_PREMIUM_REBUILD
        for a in adj_their_players + adj_seed_recv_players:
            restore_map[a["player_id"]] = 1 - PLAYER_DISCOUNT_REBUILD

        my_pool = my_players_pool + adj_my_picks
        their_pool = adj_their_players + their_picks
        seed_recv_search = adj_seed_recv_players + seed_recv_picks
        pick_note = f"your picks boosted +{int(PICK_PREMIUM_REBUILD * 100)}% -- they're rebuilding"
    else:
        their_pool = their_players + their_picks
        seed_recv_search = seed_recv

    packages = generate_packages_seeded(seed_send, seed_recv_search, my_pool, their_pool, STATE.surplus[my_rid])

    def restore(a):
        factor = restore_map.get(a["player_id"])
        return {**a, "value": int(a["value"] / factor)} if factor else a

    result_packages = [
        build_trade_view(my_rid, [restore(a) for a in s], opponent_rid, [restore(a) for a in r])
        for s, r in packages
    ]
    return jsonify({"packages": result_packages, "opponent_tier": opponent_tier, "pick_note": pick_note})


@app.route("/api/evaluate", methods=["POST"])
def api_evaluate():
    data = request.get_json(force=True) or {}
    if data.get("recv_roster_id") is None:
        abort(400, description="recv_roster_id is required")
    send_rid = int(data.get("send_roster_id") or STATE.my_roster["roster_id"])
    recv_rid = int(data["recv_roster_id"])
    get_roster(send_rid)
    get_roster(recv_rid)

    send_assets = resolve_ids(send_rid, data.get("send_ids") or [])
    recv_assets = resolve_ids(recv_rid, data.get("recv_ids") or [])

    view = build_trade_view(send_rid, send_assets, recv_rid, recv_assets)
    view["surplus_impact"] = compute_surplus_impact(send_rid, send_assets, recv_rid, recv_assets)
    return jsonify(view)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
