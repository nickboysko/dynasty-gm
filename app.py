"""
Interactive trade builder web app.

Run locally with: python app.py (127.0.0.1:5000, debug on, no auth).
In production (Render), served by gunicorn on $PORT with a password gate --
see render.yaml. Data auto-refreshes: a background ingest kicks off on boot
and on any page load once the last refresh is stale, plus a manual
"Refresh Now" button -- poll /api/update/status for progress. See
db_backup.py for how data survives Render's free-tier disk wipes.
"""

import os
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

import copy

import db
import db_backup
import ingest
from playoff_sim import compute_playoff_odds
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
    MAX_PARTNERS,
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
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-key-change-me")
app.permanent_session_lifetime = timedelta(days=30)

APP_PASSWORD = os.environ.get("APP_PASSWORD")  # auth gate is a no-op if unset (local dev)


# ---------------------------------------------------------------------------
# Startup: load league data into an in-memory STATE singleton, kept fresh by
# a background auto-refresh (see "Background auto-refresh" below) instead of
# only ever loading once.
# ---------------------------------------------------------------------------

class State:
    pass


STATE = State()
_state_lock = threading.Lock()


def load_state():
    """Builds a scratch State() from the DB, then atomically swaps it into
    the module-level STATE singleton. Safe to call repeatedly (e.g. after a
    background ingest) -- the single dict-pointer swap means readers never
    see a half-old/half-new STATE."""
    conn = db.get_connection()
    s = State()
    s.settings = load_settings(conn)
    s.fc_values = get_latest_fc_values(conn)
    s.rosters = get_rosters(conn, s.fc_values)
    pick_values = build_pick_value_table(s.fc_values)
    s.pick_assets = compute_pick_assets(conn, s.rosters, s.settings["draft_rounds"], pick_values)
    s.surplus = compute_positional_surplus(s.rosters, s.settings["starting_slots"])
    s.tiers = classify_teams(s.rosters, s.settings["starting_slots"])
    s.trends = get_value_trends(conn, days=7)
    s.my_roster = find_my_roster(s.rosters, MY_TEAM)
    s.roster_by_id = {r["roster_id"]: r for r in s.rosters}
    s.traded_picks_rows = conn.execute(
        "SELECT season, round, original_roster_id, current_roster_id FROM traded_picks"
    ).fetchall()
    s.free_agents = get_free_agents(conn, s.fc_values)
    s.playoff_odds = compute_playoff_odds(conn, s.rosters, s.settings)
    conn.close()
    with _state_lock:
        STATE.__dict__ = s.__dict__


db_backup.restore()  # no-op locally; on Render, pulls the last snapshot before init_db
db.init_db()
load_state()


# ---------------------------------------------------------------------------
# Background auto-refresh
#
# A full ingest run takes 60-120+ seconds (Sleeper rate-limit-friendly
# sleeps), so it always runs off the request thread. Kicked off at process
# boot (the common case: Render's cold start *is* the page load that woke
# it), on any page load once stale, and unconditionally via "Refresh Now".
# ---------------------------------------------------------------------------

_update_lock = threading.Lock()
UPDATE_STATUS = {"running": False, "last_success": None, "last_error": None}
AUTO_REFRESH_COOLDOWN_SECONDS = 900  # 15 min


def _do_update():
    if not _update_lock.acquire(blocking=False):
        return  # an update is already running -- don't stack another one
    UPDATE_STATUS["running"] = True
    try:
        ingest.main()
        load_state()
        db_backup.backup()  # no-op locally
        UPDATE_STATUS["last_success"] = datetime.now(timezone.utc).isoformat()
        UPDATE_STATUS["last_error"] = None
    except Exception as exc:
        UPDATE_STATUS["last_error"] = str(exc)
    finally:
        UPDATE_STATUS["running"] = False
        _update_lock.release()


def trigger_update_async():
    threading.Thread(target=_do_update, daemon=True).start()


def _last_success_stale():
    last = UPDATE_STATUS["last_success"]
    if not last:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
    return age > timedelta(seconds=AUTO_REFRESH_COOLDOWN_SECONDS)


trigger_update_async()  # kick off the first refresh as soon as the process boots


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

@app.errorhandler(HTTPException)
def handle_http_exception(e):
    return jsonify({"error": e.description}), e.code


@app.before_request
def require_login():
    """No-op if APP_PASSWORD isn't set -- keeps local `python app.py` dev
    working with zero config. Once deployed with the env var set, gates
    everything except the login page and static assets behind one shared
    password (no per-user accounts -- this is a personal tool, not a
    product)."""
    if not APP_PASSWORD:
        return
    if request.endpoint in ("login", "static"):
        return
    if not session.get("authed"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session.permanent = True
            session["authed"] = True
            return redirect(url_for("index"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
        "injury_flag": a.get("injury_flag"),
        "injury_body_part": a.get("injury_body_part"),
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
    if _last_success_stale():
        trigger_update_async()
    return render_template("index.html")


@app.route("/api/update/status")
def api_update_status():
    return jsonify(UPDATE_STATUS)


@app.route("/api/update/trigger", methods=["POST"])
def api_update_trigger():
    trigger_update_async()
    return jsonify(UPDATE_STATUS)


@app.route("/api/playoff_odds")
def api_playoff_odds():
    return jsonify(STATE.playoff_odds)


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
    my_rid = STATE.my_roster["roster_id"]
    my_info = STATE.tiers[my_rid]
    return jsonify({
        "my_roster_id": my_rid,
        "my_team": STATE.my_roster["team"],
        "my_tier": {
            "tier": my_info["tier"],
            "wins": my_info["wins"],
            "losses": my_info["losses"],
            "record_used": my_info["record_used"],
        },
        "teams": teams,
    })


@app.route("/api/roster/<int:roster_id>")
def api_roster(roster_id):
    roster = get_roster(roster_id)
    assets = get_sendable_assets(roster_id)
    info = STATE.tiers[roster_id]
    return jsonify({
        "roster_id": roster_id,
        "team": roster["team"],
        "tier": info["tier"],
        "wins": info["wins"],
        "losses": info["losses"],
        "record_used": info["record_used"],
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


def _my_weakest_by_position():
    """Lowest-value rostered player at each position -- the natural drop candidate
    if a free agent turns out to be worth more."""
    weakest = {}
    for p in STATE.my_roster["players"]:
        if p["position"] not in POSITIONS:
            continue
        if p["position"] not in weakest or p["value"] < weakest[p["position"]]["value"]:
            weakest[p["position"]] = p
    return weakest


@app.route("/api/free_agents")
def api_free_agents():
    agents = [a for a in STATE.free_agents if a["position"] in POSITIONS and a["value"] > 0]
    agents.sort(key=lambda a: a["value"], reverse=True)
    weakest_by_pos = _my_weakest_by_position()

    result = []
    for a in agents:
        trend = STATE.trends.get(a["player_id"])
        weakest = weakest_by_pos.get(a["position"])
        comparison = None
        if weakest:
            comparison = {
                "player_name": weakest["full_name"],
                "player_value": weakest["value"],
                "value_diff": a["value"] - weakest["value"],
                "worth_drop": a["value"] > weakest["value"],
            }
        result.append({
            **a,
            "trend_delta_pct": trend["delta_pct"] if trend else None,
            "roster_comparison": comparison,
        })
    return jsonify({"free_agents": result})


def _find_packages_for_opponent(my_rid, seed_send, seed_recv, opponent_rid):
    """Core seeded-package search against one specific opponent. seed_send/seed_recv
    are already-resolved asset lists. Returns {"packages", "opponent_tier", "pick_note"}."""
    opponent_roster = get_roster(opponent_rid)
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

    my_tier = STATE.tiers[my_rid]["tier"]
    packages = generate_packages_seeded(seed_send, seed_recv_search, my_pool, their_pool, STATE.surplus[my_rid], my_tier=my_tier)

    def restore(a):
        factor = restore_map.get(a["player_id"])
        return {**a, "value": int(a["value"] / factor)} if factor else a

    result_packages = [
        build_trade_view(my_rid, [restore(a) for a in s], opponent_rid, [restore(a) for a in r])
        for s, r in packages
    ]
    return {"packages": result_packages, "opponent_tier": opponent_tier, "pick_note": pick_note}


@app.route("/api/find_trades", methods=["POST"])
def api_find_trades():
    data = request.get_json(force=True) or {}
    if data.get("opponent_roster_id") is None:
        abort(400, description="opponent_roster_id is required")
    opponent_rid = int(data["opponent_roster_id"])
    my_rid = STATE.my_roster["roster_id"]
    if opponent_rid == my_rid:
        abort(400, description="opponent_roster_id cannot be your own roster")
    get_roster(opponent_rid)

    seed_send = resolve_ids(my_rid, data.get("seed_send_ids") or [])
    seed_recv = resolve_ids(opponent_rid, data.get("seed_recv_ids") or [])

    return jsonify(_find_packages_for_opponent(my_rid, seed_send, seed_recv, opponent_rid))


@app.route("/api/find_trades_all", methods=["POST"])
def api_find_trades_all():
    """Search across the top-ranked partners at once, using only my-side seeds --
    for when you want to see what multiple teams would offer for the same players,
    rather than committing to one opponent first."""
    data = request.get_json(force=True) or {}
    my_rid = STATE.my_roster["roster_id"]
    seed_send = resolve_ids(my_rid, data.get("seed_send_ids") or [])

    partners = rank_trade_partners(STATE.my_roster, STATE.rosters, STATE.surplus)
    results = []
    for score, partner in partners[:MAX_PARTNERS]:
        opponent_rid = partner["roster_id"]
        info = STATE.tiers[opponent_rid]
        pkg_data = _find_packages_for_opponent(my_rid, seed_send, [], opponent_rid)
        if pkg_data["packages"]:
            results.append({
                "opponent_roster_id": opponent_rid,
                "team": partner["team"],
                "tier": pkg_data["opponent_tier"],
                "wins": info["wins"],
                "losses": info["losses"],
                "record_used": info["record_used"],
                "fit_score": score,
                "pick_note": pkg_data["pick_note"],
                "packages": pkg_data["packages"],
            })
    return jsonify({"results": results})


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
    # Render sets $PORT and this block is never hit anyway (gunicorn imports
    # app:app directly) -- this fallback just keeps `python app.py` working
    # unchanged for local dev while being sane if ever run directly in prod.
    port = int(os.environ.get("PORT", 5000))
    if os.environ.get("PORT"):
        app.run(host="0.0.0.0", port=port)
    else:
        app.run(host="127.0.0.1", port=port, debug=True)
