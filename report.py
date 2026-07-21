from collections import defaultdict
from datetime import date
from tabulate import tabulate

import db
from utils import (
    POSITIONS,
    assign_starters,
    build_pick_value_table,
    classify_teams,
    compute_pick_assets,
    get_latest_fc_values,
    get_rosters,
    get_value_trends,
    load_settings,
)


# ---------------------------------------------------------------------------
# Report sections
#
# Each section has a compute_* function returning plain data (reused by
# app.py's /api/report) and a section_* function that prints it via tabulate.
# ---------------------------------------------------------------------------

def compute_total_value(rosters):
    """1. Total roster value ranking (all slots included)."""
    return sorted(
        [{"team": r["team"], "total_value": sum(p["value"] for p in r["players"])} for r in rosters],
        key=lambda x: x["total_value"], reverse=True,
    )


def section_total_value(rosters):
    print("\n=== 1. Total Roster Value Ranking ===")
    rows = compute_total_value(rosters)
    table = [[i + 1, row["team"], f"{row['total_value']:,}"] for i, row in enumerate(rows)]
    print(tabulate(table, headers=["Rank", "Team", "Total Value"], tablefmt="simple"))


def compute_position_value(rosters):
    """2. Per-team value by position (active players only) vs. league median."""
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

    teams = []
    for r in sorted(rosters, key=lambda r: sum(team_pos[r["team"]].values()), reverse=True):
        pos_values = {}
        for pos in POSITIONS:
            val = team_pos[r["team"]].get(pos, 0)
            pos_values[pos] = {"value": val, "diff": val - medians[pos]}
        teams.append({"team": r["team"], "positions": pos_values})

    return {"medians": medians, "teams": teams}


def section_position_value(rosters):
    print("\n=== 2. Position Value vs. League Median ===")
    data = compute_position_value(rosters)
    medians = data["medians"]

    print("League medians: " + "  ".join(f"{pos}: {int(medians[pos]):,}" for pos in POSITIONS))

    headers = ["Team"] + [f"{pos} (vs med)" for pos in POSITIONS]
    table = []
    for team in data["teams"]:
        row = [team["team"]]
        for pos in POSITIONS:
            val = team["positions"][pos]["value"]
            diff = team["positions"][pos]["diff"]
            sign = "+" if diff >= 0 else ""
            row.append(f"{val:,} ({sign}{int(diff):,})")
        table.append(row)
    print(tabulate(table, headers=headers, tablefmt="simple"))


def compute_starter_vs_bench(rosters, starting_slots):
    """3. Starter vs. bench value. Starters assigned from active slot only; taxi/IR always bench."""
    rows = []
    for r in rosters:
        active = [p for p in r["players"] if p["slot"] == "active"]
        starters, active_bench = assign_starters(active, starting_slots)
        bench = active_bench + [p for p in r["players"] if p["slot"] in ("taxi", "ir")]

        sv = sum(p["value"] for p in starters)
        bv = sum(p["value"] for p in bench)
        total = sv + bv
        bench_pct = bv / total * 100 if total else 0
        rows.append({"team": r["team"], "starter_value": sv, "bench_value": bv, "bench_pct": bench_pct})

    rows.sort(key=lambda x: x["starter_value"], reverse=True)
    return rows


def section_starter_vs_bench(rosters, starting_slots):
    print("\n=== 3. Starter vs. Bench Value ===")
    rows = compute_starter_vs_bench(rosters, starting_slots)
    table = [[r["team"], f"{r['starter_value']:,}", f"{r['bench_value']:,}", f"{r['bench_pct']:.0f}%"] for r in rows]
    print(tabulate(table, headers=["Team", "Starter Value", "Bench Value", "Bench %"], tablefmt="simple"))


def compute_age(rosters):
    """4. Value-weighted average age per team (all players with known age and non-zero value)."""
    rows = []
    for r in rosters:
        eligible = [p for p in r["players"] if p["age"] is not None and p["value"] > 0]
        if not eligible:
            rows.append({"team": r["team"], "wtd_age": None})
            continue
        total_weight = sum(p["value"] for p in eligible)
        wtd_age = sum(p["age"] * p["value"] for p in eligible) / total_weight
        rows.append({"team": r["team"], "wtd_age": wtd_age})

    rows.sort(key=lambda x: (x["wtd_age"] is None, x["wtd_age"] or 0))
    return rows


def section_age(rosters):
    print("\n=== 4. Value-Weighted Average Age ===")
    rows = compute_age(rosters)
    table = [[r["team"], f"{r['wtd_age']:.1f}" if r["wtd_age"] is not None else "N/A"] for r in rows]
    print(tabulate(table, headers=["Team", "Wtd Avg Age"], tablefmt="simple"))


def compute_pick_capital(rosters, fc_values, draft_rounds, traded_picks_rows):
    """
    5. Draft pick capital per team.

    Ownership is computed by starting with each team owning all their own picks
    for upcoming seasons, then applying traded_picks to reflect trades.
    Values come from FantasyCalc pick entries averaged by (season, round).
    """
    roster_ids = [r["roster_id"] for r in rosters]
    team_by_rid = {r["roster_id"]: r["team"] for r in rosters}

    # Only count seasons after the current calendar year — the current year's
    # rookie draft has already happened by the time the NFL season is underway.
    start_year = date.today().year + 1
    future_seasons = [str(y) for y in range(start_year, start_year + 3)]

    pick_values = build_pick_value_table(fc_values)

    # Initialize: every team owns all their own picks for future seasons
    ownership = {
        (season, rnd, rid): rid
        for season in future_seasons
        for rnd in range(1, draft_rounds + 1)
        for rid in roster_ids
    }

    for t in traded_picks_rows:
        key = (t["season"], t["round"], t["original_roster_id"])
        if key in ownership:
            ownership[key] = t["current_roster_id"]

    team_picks = defaultdict(list)
    for (season, rnd, original), current in ownership.items():
        val = pick_values.get((season, rnd), 0)
        team_picks[current].append((season, rnd, original, val))

    rows = []
    for r in rosters:
        rid = r["roster_id"]
        picks = team_picks.get(rid, [])
        total_val = sum(p[3] for p in picks)
        received = [
            {"season": s, "round": rn, "from_team": team_by_rid.get(orig, str(orig))}
            for s, rn, orig, _ in sorted(picks) if orig != rid
        ]
        rows.append({"team": r["team"], "pick_value": total_val, "num_picks": len(picks), "received": received})

    rows.sort(key=lambda x: x["pick_value"], reverse=True)
    return {"teams": rows, "picks_unparsed": not pick_values}


def section_pick_capital(rosters, fc_values, draft_rounds, traded_picks_rows):
    print("\n=== 5. Draft Pick Capital ===")
    data = compute_pick_capital(rosters, fc_values, draft_rounds, traded_picks_rows)

    table = []
    for row in data["teams"]:
        recv_str = ", ".join(
            f"{p['season']} Rd{p['round']} ({p['from_team']})" for p in row["received"]
        ) if row["received"] else "-"
        table.append([row["team"], f"{row['pick_value']:,}", row["num_picks"], recv_str])

    print(tabulate(table, headers=["Team", "Pick Value", "# Picks", "Received Picks"], tablefmt="simple"))

    if data["picks_unparsed"]:
        print("\n  Note: FantasyCalc pick entries could not be parsed -- pick values shown as 0.")
        print("  Pick counts still reflect traded pick ownership.")


def compute_value_movers(trends):
    """6. Top value risers and fallers over the past 7 days — buy low / sell high signals."""
    if not trends:
        return None

    skill = {pid: d for pid, d in trends.items() if d["position"] in POSITIONS}
    risers = sorted(skill.items(), key=lambda x: x[1]["delta_pct"], reverse=True)[:10]
    fallers = sorted(skill.items(), key=lambda x: x[1]["delta_pct"])[:10]

    return {
        "risers": [d for _, d in risers if d["delta_pct"] > 0],
        "fallers": [d for _, d in fallers if d["delta_pct"] < 0],
    }


def section_value_movers(trends):
    print("\n=== 6. Value Movers (7-Day Trend) ===")
    data = compute_value_movers(trends)

    if data is None:
        print("  No historical data yet -- run ingest.py daily for at least 2 days to see trends.")
        return

    if data["risers"]:
        up = [(d["name"], d["position"], f"{d['prev']:,}", f"{d['current']:,}", f"+{d['delta_pct']:.1f}%") for d in data["risers"]]
        print("\nTop Risers -- consider SELLING HIGH:")
        print(tabulate(up, headers=["Player", "Pos", "7d Ago", "Now", "Change"], tablefmt="simple"))

    if data["fallers"]:
        down = [(d["name"], d["position"], f"{d['prev']:,}", f"{d['current']:,}", f"{d['delta_pct']:.1f}%") for d in data["fallers"]]
        print("\nTop Fallers -- consider BUYING LOW:")
        print(tabulate(down, headers=["Player", "Pos", "7d Ago", "Now", "Change"], tablefmt="simple"))


def compute_strategy(rosters, starting_slots, pick_assets):
    """7. Win-now vs. rebuild assessment for every team."""
    tiers = classify_teams(rosters, starting_slots)
    use_record = any(info.get("record_used") for info in tiers.values())

    rows = []
    for r in rosters:
        rid = r["roster_id"]
        info = tiers[rid]
        active = [p for p in r["players"] if p["slot"] == "active"]
        starters, _ = assign_starters(active, starting_slots)
        sv = sum(p["value"] for p in starters)
        tv = sum(p["value"] for p in r["players"])
        pv = sum(p["value"] for p in pick_assets.get(rid, []))
        record = f"{r['wins']}-{r['losses']}" if use_record else None
        rows.append({
            "team": r["team"], "tier": info["tier"], "score": info["score"], "record": record,
            "starter_value": sv, "total_value": tv, "pick_capital": pv,
        })

    rows.sort(key=lambda x: x["score"], reverse=True)
    return {"use_record": use_record, "teams": rows}


def section_strategy(rosters, starting_slots, pick_assets):
    print("\n=== 7. Strategy Assessment ===")
    data = compute_strategy(rosters, starting_slots, pick_assets)

    headers = ["Team", "Tier", "Score", "Record", "Starter Val", "Total Val", "Pick Capital"]
    table = [
        [row["team"], row["tier"], f"{row['score']:.0%}", row["record"] or "-",
         f"{row['starter_value']:,}", f"{row['total_value']:,}", f"{row['pick_capital']:,}"]
        for row in data["teams"]
    ]
    print(tabulate(table, headers=headers, tablefmt="simple"))
    basis = "40% starter rank + 20% total rank + 40% win rate" if data["use_record"] else "60% starter rank + 40% total rank (offseason -- no record)"
    print(f"\nTier basis: {basis}")
    print("Contending (>=60%) = compete now  |  Middle = flexible  |  Rebuilding (<=35%) = accumulate picks")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    db.init_db()
    conn = db.get_connection()
    settings = load_settings(conn)
    fc_values = get_latest_fc_values(conn)
    rosters = get_rosters(conn, fc_values)
    trends = get_value_trends(conn, days=7)
    traded_picks_rows = conn.execute(
        "SELECT season, round, original_roster_id, current_roster_id FROM traded_picks"
    ).fetchall()

    fmt_str = f"{'Superflex' if settings['superflex'] else '1QB'}, {settings['ppr']} PPR"
    if settings["te_premium"]:
        fmt_str += f", TE+{settings['te_premium']}"
    slots = settings["starting_slots"]

    print("=== Dynasty GM -- Roster Strength Report ===")
    print(f"Format: {fmt_str}")
    print(f"Starting slots ({len(slots)}): {', '.join(slots)}")

    pick_values = build_pick_value_table(fc_values)
    pick_assets = compute_pick_assets(conn, rosters, settings["draft_rounds"], pick_values)

    section_total_value(rosters)
    section_position_value(rosters)
    section_starter_vs_bench(rosters, slots)
    section_age(rosters)
    section_pick_capital(rosters, fc_values, settings["draft_rounds"], traded_picks_rows)
    section_value_movers(trends)
    section_strategy(rosters, slots, pick_assets)

    conn.close()


if __name__ == "__main__":
    main()
