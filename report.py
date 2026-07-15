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


def section_value_movers(conn):
    """6. Top value risers and fallers over the past 7 days — buy low / sell high signals."""
    print("\n=== 6. Value Movers (7-Day Trend) ===")
    trends = get_value_trends(conn, days=7)

    if not trends:
        print("  No historical data yet — run ingest.py daily for at least 2 days to see trends.")
        return

    skill = {pid: d for pid, d in trends.items() if d["position"] in POSITIONS}
    risers = sorted(skill.items(), key=lambda x: x[1]["delta_pct"], reverse=True)[:10]
    fallers = sorted(skill.items(), key=lambda x: x[1]["delta_pct"])[:10]

    up = [(d["name"], d["position"], f"{d['prev']:,}", f"{d['current']:,}", f"+{d['delta_pct']:.1f}%") for _, d in risers if d["delta_pct"] > 0]
    if up:
        print("\nTop Risers — consider SELLING HIGH:")
        print(tabulate(up, headers=["Player", "Pos", "7d Ago", "Now", "Change"], tablefmt="simple"))

    down = [(d["name"], d["position"], f"{d['prev']:,}", f"{d['current']:,}", f"{d['delta_pct']:.1f}%") for _, d in fallers if d["delta_pct"] < 0]
    if down:
        print("\nTop Fallers — consider BUYING LOW:")
        print(tabulate(down, headers=["Player", "Pos", "7d Ago", "Now", "Change"], tablefmt="simple"))


def section_strategy(rosters, starting_slots, pick_assets):
    """7. Win-now vs. rebuild assessment for every team."""
    print("\n=== 7. Strategy Assessment ===")
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
        record = f"{r['wins']}-{r['losses']}" if use_record else "-"
        rows.append([r["team"], info["tier"], f"{info['score']:.0%}", record, f"{sv:,}", f"{tv:,}", f"{pv:,}"])

    rows.sort(key=lambda x: x[2], reverse=True)
    headers = ["Team", "Tier", "Score", "Record", "Starter Val", "Total Val", "Pick Capital"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))
    basis = "40% starter rank + 20% total rank + 40% win rate" if use_record else "60% starter rank + 40% total rank (offseason — no record)"
    print(f"\nTier basis: {basis}")
    print("Contending (≥60%) = compete now  |  Middle = flexible  |  Rebuilding (≤35%) = accumulate picks")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    db.init_db()
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

    pick_values = build_pick_value_table(fc_values)
    pick_assets = compute_pick_assets(conn, rosters, settings["draft_rounds"], pick_values)

    section_total_value(rosters)
    section_position_value(rosters)
    section_starter_vs_bench(rosters, slots)
    section_age(rosters)
    section_pick_capital(conn, rosters, fc_values, settings["draft_rounds"], settings["season"])
    section_value_movers(conn)
    section_strategy(rosters, slots, pick_assets)

    conn.close()


if __name__ == "__main__":
    main()
