import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import db

POSITIONS = ["QB", "RB", "WR", "TE"]


def load_settings(conn):
    rows = conn.execute("SELECT key, value FROM league_settings").fetchall()
    if not rows:
        raise SystemExit("No league settings found. Run ingest.py first.")
    s = {r["key"]: r["value"] for r in rows}
    return {
        "superflex": s.get("superflex", "False") == "True",
        "num_qbs": int(s.get("num_qbs", 1)),
        "ppr": float(s.get("ppr", 0)),
        "te_premium": float(s.get("te_premium", 0)),
        "starting_slots": json.loads(s.get("starting_slots", "[]")),
        "draft_rounds": int(s.get("draft_rounds", 5)),
        "season": s.get("season", str(date.today().year)),
    }


def get_latest_fc_values(conn):
    """Returns dict: player_id -> {value, name, position}"""
    rows = conn.execute("""
        SELECT fv.player_id, fv.name, fv.position, fv.value
        FROM fc_values fv
        JOIN (
            SELECT player_id, MAX(fetched_at) AS max_fa
            FROM fc_values
            GROUP BY player_id
        ) latest ON fv.player_id = latest.player_id AND fv.fetched_at = latest.max_fa
    """).fetchall()
    return {r["player_id"]: dict(r) for r in rows}


def get_rosters(conn, fc_values):
    """Returns list of roster dicts, each with a 'players' list enriched with FC values."""
    rosters = conn.execute("""
        SELECT r.roster_id, r.wins, r.losses, r.ties, r.fpts,
               u.display_name, u.team_name
        FROM rosters r
        LEFT JOIN users u ON r.owner_id = u.user_id
        ORDER BY r.roster_id
    """).fetchall()

    result = []
    for roster in rosters:
        rid = roster["roster_id"]
        team = roster["team_name"] or roster["display_name"] or f"Roster {rid}"

        players = conn.execute("""
            SELECT p.player_id, p.full_name, p.position, p.team, p.age, rp.slot
            FROM roster_players rp
            JOIN players p ON rp.player_id = p.player_id
            WHERE rp.roster_id = ?
        """, (rid,)).fetchall()

        player_list = [
            {
                "player_id": p["player_id"],
                "full_name": p["full_name"] or p["player_id"],
                "position": p["position"],
                "team": p["team"],
                "age": p["age"],
                "slot": p["slot"],
                "value": fc_values.get(p["player_id"], {}).get("value", 0),
            }
            for p in players
        ]

        result.append({
            "roster_id": rid,
            "team": team,
            "players": player_list,
            "wins": roster["wins"] or 0,
            "losses": roster["losses"] or 0,
            "ties": roster["ties"] or 0,
            "fpts": roster["fpts"] or 0,
        })
    return result


def assign_starters(players, starting_slots):
    """
    Greedy starter assignment that respects position eligibility.
    Fill positional slots first, then FLEX (RB/WR/TE), then SUPER_FLEX (QB/RB/WR/TE).
    Returns (starters, bench).
    """
    slot_counts = Counter(starting_slots)

    by_pos = defaultdict(list)
    for p in players:
        by_pos[p["position"]].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: x["value"], reverse=True)

    used = set()
    starters = []

    for slot in ["QB", "RB", "WR", "TE", "K", "DEF"]:
        for p in by_pos.get(slot, [])[:slot_counts.get(slot, 0)]:
            starters.append(p)
            used.add(p["player_id"])

    flex_pool = sorted(
        [p for pos in ["RB", "WR", "TE"] for p in by_pos.get(pos, []) if p["player_id"] not in used],
        key=lambda x: x["value"], reverse=True,
    )
    for p in flex_pool[:slot_counts.get("FLEX", 0)]:
        starters.append(p)
        used.add(p["player_id"])

    sf_pool = sorted(
        [p for pos in ["QB", "RB", "WR", "TE"] for p in by_pos.get(pos, []) if p["player_id"] not in used],
        key=lambda x: x["value"], reverse=True,
    )
    for p in sf_pool[:slot_counts.get("SUPER_FLEX", 0)]:
        starters.append(p)
        used.add(p["player_id"])

    bench = [p for p in players if p["player_id"] not in used]
    return starters, bench


def get_value_trends(conn, days=7):
    """
    Returns dict: player_id -> {current, prev, delta, delta_pct, name, position}.
    Compares the latest fc_values fetch vs. the oldest fetch within `days` days.
    Returns {} if fewer than 2 distinct fetch dates exist in the window.
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    fetch_dates = conn.execute(
        "SELECT DISTINCT substr(fetched_at, 1, 10) FROM fc_values WHERE fetched_at >= ? ORDER BY 1",
        (cutoff,),
    ).fetchall()
    if len(fetch_dates) < 2:
        return {}

    prev_rows = conn.execute("""
        SELECT fv.player_id, fv.value, fv.name, fv.position
        FROM fc_values fv
        JOIN (
            SELECT player_id, MIN(fetched_at) AS min_fa
            FROM fc_values
            WHERE fetched_at >= ?
            GROUP BY player_id
        ) earliest ON fv.player_id = earliest.player_id AND fv.fetched_at = earliest.min_fa
    """, (cutoff,)).fetchall()

    prev = {r["player_id"]: r["value"] for r in prev_rows}
    prev_meta = {r["player_id"]: (r["name"], r["position"]) for r in prev_rows}

    current_rows = conn.execute("""
        SELECT fv.player_id, fv.value, fv.name, fv.position
        FROM fc_values fv
        JOIN (
            SELECT player_id, MAX(fetched_at) AS max_fa
            FROM fc_values
            GROUP BY player_id
        ) latest ON fv.player_id = latest.player_id AND fv.fetched_at = latest.max_fa
    """).fetchall()

    result = {}
    for r in current_rows:
        pid = r["player_id"]
        if pid not in prev:
            continue
        prev_val = prev[pid]
        curr_val = r["value"]
        if prev_val == curr_val or prev_val == 0:
            continue
        delta = curr_val - prev_val
        delta_pct = delta / prev_val * 100
        result[pid] = {
            "current": curr_val,
            "prev": prev_val,
            "delta": delta,
            "delta_pct": delta_pct,
            "name": r["name"] or prev_meta.get(pid, (pid, "?"))[0],
            "position": r["position"] or prev_meta.get(pid, (pid, "?"))[1],
        }
    return result


def classify_teams(rosters, starting_slots):
    """
    Returns dict: roster_id -> {"score": float 0-1, "tier": str}.
    Score = 60% starter value rank + 40% total roster value rank.
    High score = Contending; low score = Rebuilding.
    """
    CONTEND_THRESHOLD = 0.60
    REBUILD_THRESHOLD = 0.35

    starter_vals = {}
    for r in rosters:
        active = [p for p in r["players"] if p["slot"] == "active"]
        starters, _ = assign_starters(active, starting_slots)
        starter_vals[r["roster_id"]] = sum(p["value"] for p in starters)

    total_vals = {r["roster_id"]: sum(p["value"] for p in r["players"]) for r in rosters}

    n = len(rosters)
    sv_sorted = sorted(starter_vals, key=lambda rid: starter_vals[rid], reverse=True)
    tv_sorted = sorted(total_vals, key=lambda rid: total_vals[rid], reverse=True)
    sv_rank = {rid: i for i, rid in enumerate(sv_sorted)}
    tv_rank = {rid: i for i, rid in enumerate(tv_sorted)}

    # Use win rate when season is underway; fall back to value-only in offseason
    max_games = max((r["wins"] + r["losses"] + r["ties"]) for r in rosters)
    use_record = max_games > 0

    result = {}
    for r in rosters:
        rid = r["roster_id"]
        sv_score = 1.0 - sv_rank[rid] / (n - 1) if n > 1 else 1.0
        tv_score = 1.0 - tv_rank[rid] / (n - 1) if n > 1 else 1.0

        if use_record:
            games = r["wins"] + r["losses"] + r["ties"]
            wr = r["wins"] / games if games > 0 else 0.5
            # 40% starter rank, 20% total rank, 40% win rate
            score = sv_score * 0.40 + tv_score * 0.20 + wr * 0.40
        else:
            score = sv_score * 0.60 + tv_score * 0.40

        tier = "Contending" if score >= CONTEND_THRESHOLD else "Rebuilding" if score <= REBUILD_THRESHOLD else "Middle"
        result[rid] = {
            "score": score,
            "tier": tier,
            "wins": r["wins"],
            "losses": r["losses"],
            "record_used": use_record,
        }

    return result


def compute_pick_assets(conn, rosters, draft_rounds, pick_values):
    """Returns dict: roster_id -> list of pick asset dicts (future seasons only)."""
    roster_ids = [r["roster_id"] for r in rosters]
    team_by_rid = {r["roster_id"]: r["team"] for r in rosters}

    start_year = date.today().year + 1
    future_seasons = [str(y) for y in range(start_year, start_year + 3)]

    ownership = {
        (season, rnd, rid): rid
        for season in future_seasons
        for rnd in range(1, draft_rounds + 1)
        for rid in roster_ids
    }

    for t in conn.execute(
        "SELECT season, round, original_roster_id, current_roster_id FROM traded_picks"
    ).fetchall():
        key = (t["season"], t["round"], t["original_roster_id"])
        if key in ownership:
            ownership[key] = t["current_roster_id"]

    team_picks = defaultdict(list)
    for (season, rnd, original), current in ownership.items():
        val = pick_values.get((season, rnd), 0)
        orig_team = team_by_rid.get(original, str(original))
        label = f"{season} Rd{rnd}" if original == current else f"{season} Rd{rnd} ({orig_team})"
        team_picks[current].append({
            "player_id": f"PICK_{season}_{rnd}_{original}",
            "full_name": label,
            "position": "PICK",
            "team": None,
            "age": None,
            "slot": "active",
            "value": val,
        })

    return team_picks


def build_pick_value_table(fc_values):
    """
    Parses FantasyCalc pick entries into a (year_str, round_int) -> avg_value lookup.
    Handles early/mid/late variants by averaging them.
    """
    ordinals = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5}
    bucket = defaultdict(list)

    for pid, data in fc_values.items():
        pos = (data.get("position") or "").upper()
        name = data.get("name") or ""
        is_pick = pos == "PICK" or "pick" in pid.lower()
        if not is_pick:
            continue

        year_m = re.search(r"(20\d{2})", name)
        round_m = re.search(r"\b(\d+)(?:st|nd|rd|th)\b", name, re.IGNORECASE)
        if not year_m or not round_m:
            continue

        year = year_m.group(1)
        round_key = round_m.group(1).lower() + (
            "st" if round_m.group(1) == "1" else
            "nd" if round_m.group(1) == "2" else
            "rd" if round_m.group(1) == "3" else "th"
        )
        round_num = ordinals.get(round_key)
        if round_num:
            bucket[(year, round_num)].append(data["value"])

    return {k: int(sum(v) / len(v)) for k, v in bucket.items()}
