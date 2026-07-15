import json
import re
from collections import Counter, defaultdict
from datetime import date

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
        SELECT r.roster_id, u.display_name, u.team_name
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

        result.append({"roster_id": rid, "team": team, "players": player_list})
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
