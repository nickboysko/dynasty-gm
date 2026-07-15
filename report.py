import json
import re
from collections import Counter, defaultdict
from datetime import date

from tabulate import tabulate

import db

POSITIONS = ["QB", "RB", "WR", "TE"]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Starter assignment
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def section_total_value(rosters):
    """1. Total roster value ranking (all slots included)."""
    print("\n=== 1. Total Roster Value Ranking ===")
    rows = sorted(
        [(r["team"], sum(p["value"] for p in r["players"])) for r in rosters],
        key=lambda x: x[1], reverse=True,
    )
    table = [[i + 1, team, f"{val:,}"] for i, (team, val) in enumerate(rows)]
    print(tabulate(table, headers=["Rank", "Team", "Total Value"], tablefmt="simple"))


def section_position_value(rosters):
    """2. Per-team value by position (active players only) vs. league median."""
    print("\n=== 2. Position Value vs. League Median ===")

    team_pos = {}
    for r in rosters:
        pos_val = defaultdict(int)
        for p in r["players"]:
            if p["slot"] == "active" and p["position"] in POSITIONS:
                pos_val[p["position"]] += p["value"]
        team_pos[r["team"]] = pos_val

    medians = {}
    for pos in POSITIONS:
        vals = sorted(team_pos[t].get(pos, 0) for t in team_pos)
        n = len(vals)
        medians[pos] = (vals[n // 2 - 1] + vals[n // 2]) / 2 if n % 2 == 0 else vals[n // 2]

    print("League medians: " + "  ".join(f"{pos}: {int(medians[pos]):,}" for pos in POSITIONS))

    headers = ["Team"] + [f"{pos} (vs med)" for pos in POSITIONS]
    rows = sorted(rosters, key=lambda r: sum(team_pos[r["team"]].values()), reverse=True)
    table = []
    for r in rows:
        row = [r["team"]]
        for pos in POSITIONS:
            val = team_pos[r["team"]].get(pos, 0)
            diff = val - medians[pos]
            sign = "+" if diff >= 0 else ""
            row.append(f"{val:,} ({sign}{int(diff):,})")
        table.append(row)
    print(tabulate(table, headers=headers, tablefmt="simple"))


def section_starter_vs_bench(rosters, starting_slots):
    """3. Starter vs. bench value. Starters assigned from active slot only; taxi/IR always bench."""
    print("\n=== 3. Starter vs. Bench Value ===")
    rows = []
    for r in rosters:
        active = [p for p in r["players"] if p["slot"] == "active"]
        starters, active_bench = assign_starters(active, starting_slots)
        bench = active_bench + [p for p in r["players"] if p["slot"] in ("taxi", "ir")]

        sv = sum(p["value"] for p in starters)
        bv = sum(p["value"] for p in bench)
        total = sv + bv
        bench_pct = bv / total * 100 if total else 0
        rows.append((r["team"], sv, bv, bench_pct))

    rows.sort(key=lambda x: x[1], reverse=True)
    table = [[team, f"{sv:,}", f"{bv:,}", f"{bp:.0f}%"] for team, sv, bv, bp in rows]
    print(tabulate(table, headers=["Team", "Starter Value", "Bench Value", "Bench %"], tablefmt="simple"))


def section_age(rosters):
    """4. Value-weighted average age per team (all players with known age and non-zero value)."""
    print("\n=== 4. Value-Weighted Average Age ===")
    rows = []
    for r in rosters:
        eligible = [p for p in r["players"] if p["age"] is not None and p["value"] > 0]
        if not eligible:
            rows.append((r["team"], None))
            continue
        total_weight = sum(p["value"] for p in eligible)
        wtd_age = sum(p["age"] * p["value"] for p in eligible) / total_weight
        rows.append((r["team"], wtd_age))

    rows.sort(key=lambda x: (x[1] is None, x[1] or 0))
    table = [[team, f"{age:.1f}" if age is not None else "N/A"] for team, age in rows]
    print(tabulate(table, headers=["Team", "Wtd Avg Age"], tablefmt="simple"))


def build_pick_value_table(fc_values):
    """
    Parses FantasyCalc pick entries into a (year_str, round_int) -> avg_value lookup.
    Handles early/mid/late variants by averaging them.
    Picks are identified by position == "PICK" or "pick" in the player_id.
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


def section_pick_capital(conn, rosters, fc_values, draft_rounds, league_season):
    """
    5. Draft pick capital per team.

    Ownership is computed by starting with each team owning all their own picks
    for upcoming seasons, then applying traded_picks to reflect trades.
    Values come from FantasyCalc pick entries averaged by (season, round).
    """
    print("\n=== 5. Draft Pick Capital ===")

    roster_ids = [r["roster_id"] for r in rosters]
    team_by_rid = {r["roster_id"]: r["team"] for r in rosters}

    # Only count seasons after the current calendar year — the current year's
    # rookie draft has already happened by the time the NFL season is underway.
    start_year = date.today().year + 1
    future_seasons = [str(y) for y in range(start_year, start_year + 3)]

    pick_values = build_pick_value_table(fc_values)

    # Initialize: every team owns all their own picks for future seasons
    # Key: (season, round, original_roster_id) -> current_roster_id
    ownership = {
        (season, rnd, rid): rid
        for season in future_seasons
        for rnd in range(1, draft_rounds + 1)
        for rid in roster_ids
    }

    # Apply trades: Sleeper original_roster_id = original team, current_roster_id = current holder
    traded = conn.execute(
        "SELECT season, round, original_roster_id, current_roster_id FROM traded_picks"
    ).fetchall()
    for t in traded:
        key = (t["season"], t["round"], t["original_roster_id"])
        if key in ownership:
            ownership[key] = t["current_roster_id"]

    # Aggregate picks per team
    team_picks = defaultdict(list)
    for (season, rnd, original), current in ownership.items():
        val = pick_values.get((season, rnd), 0)
        team_picks[current].append((season, rnd, original, val))

    rows = []
    for r in rosters:
        rid = r["roster_id"]
        picks = team_picks.get(rid, [])
        total_val = sum(p[3] for p in picks)
        # Picks received from other teams (original owner != this team)
        received = [(s, rn, orig) for s, rn, orig, _ in picks if orig != rid]
        rows.append((r["team"], total_val, len(picks), received))

    rows.sort(key=lambda x: x[1], reverse=True)
    table = []
    for team, val, count, received in rows:
        recv_str = ", ".join(
            f"{s} Rd{rn} ({team_by_rid.get(orig, str(orig))})"
            for s, rn, orig in sorted(received)
        ) if received else "-"
        table.append([team, f"{val:,}", count, recv_str])

    print(tabulate(table, headers=["Team", "Pick Value", "# Picks", "Received Picks"], tablefmt="simple"))

    if not pick_values:
        print("\n  Note: FantasyCalc pick entries could not be parsed — pick values shown as 0.")
        print("  Pick counts still reflect traded pick ownership.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    conn = db.get_connection()
    settings = load_settings(conn)
    fc_values = get_latest_fc_values(conn)
    rosters = get_rosters(conn, fc_values)

    fmt_str = f"{'Superflex' if settings['superflex'] else '1QB'}, {settings['ppr']} PPR"
    if settings["te_premium"]:
        fmt_str += f", TE+{settings['te_premium']}"
    slots = settings["starting_slots"]

    print("=== Dynasty GM — Roster Strength Report ===")
    print(f"Format: {fmt_str}")
    print(f"Starting slots ({len(slots)}): {', '.join(slots)}")

    section_total_value(rosters)
    section_position_value(rosters)
    section_starter_vs_bench(rosters, slots)
    section_age(rosters)
    section_pick_capital(conn, rosters, fc_values, settings["draft_rounds"], settings["season"])

    conn.close()


if __name__ == "__main__":
    main()
