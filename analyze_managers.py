"""
Usage: python analyze_managers.py

Profiles each manager's trade behavior to identify exploitable tendencies:
  1. Future pick capital position (accumulating vs. depleting)
  2. Historical trade value exchanged (who overpays — those are your prime targets)
  3. Sell high: your roster players trending up in value
  4. Buy low: other rosters' players trending down in value
"""

import json
from collections import defaultdict
from datetime import date

from tabulate import tabulate

import db
from utils import (
    POSITIONS,
    build_pick_value_table,
    classify_teams,
    compute_pick_assets,
    get_latest_fc_values,
    get_rosters,
    get_value_trends,
    load_settings,
)

MY_TEAM = "nboysko"


def pick_capital_positions(conn, rosters, settings):
    """Net future pick position per team: acquired picks minus picks traded away."""
    team_by_rid = {r["roster_id"]: r["team"] for r in rosters}
    start_year = date.today().year + 1
    future_seasons = {str(y) for y in range(start_year, start_year + 3)}

    acquired = defaultdict(int)
    gave_away = defaultdict(int)

    for row in conn.execute(
        "SELECT season, round, original_roster_id, current_roster_id FROM traded_picks"
    ).fetchall():
        if row["season"] not in future_seasons:
            continue
        orig = row["original_roster_id"]
        curr = row["current_roster_id"]
        if orig != curr:
            gave_away[orig] += 1
            acquired[curr] += 1

    rows = []
    for r in rosters:
        rid = r["roster_id"]
        net = acquired[rid] - gave_away[rid]
        label = "Accumulating" if net > 0 else "Depleting" if net < 0 else "Neutral"
        rows.append([r["team"], acquired[rid], gave_away[rid], f"{net:+d}", label])

    rows.sort(key=lambda x: int(x[3]), reverse=True)
    return rows


def trade_value_analysis(conn, rosters, fc_values, pick_values):
    """
    For each completed trade transaction, compute the FC value each team sent and received.
    Returns None if no transaction data, empty list if no trades.
    """
    txns = conn.execute(
        "SELECT adds, drops, draft_picks, roster_ids FROM transactions"
        " WHERE type='trade' AND status='complete'"
    ).fetchall()

    if not txns:
        return None

    stats = defaultdict(lambda: {"trades": 0, "rcvd": 0, "sent": 0})

    for txn in txns:
        adds = json.loads(txn["adds"] or "{}")     # player_id -> new roster_id
        drops = json.loads(txn["drops"] or "{}")   # player_id -> old roster_id
        picks = json.loads(txn["draft_picks"] or "[]")
        involved = set(json.loads(txn["roster_ids"] or "[]"))

        for rid in involved:
            stats[rid]["trades"] += 1

        # Player values exchanged
        for pid, recv_rid in adds.items():
            val = fc_values.get(pid, {}).get("value", 0)
            stats[recv_rid]["rcvd"] += val

        for pid, send_rid in drops.items():
            val = fc_values.get(pid, {}).get("value", 0)
            stats[send_rid]["sent"] += val

        # Pick values exchanged
        for p in picks:
            season = str(p.get("season", ""))
            rnd = int(p.get("round", 0))
            val = pick_values.get((season, rnd), 0)
            recv_rid = p.get("owner_id")
            send_rid = p.get("previous_owner_id")
            if recv_rid:
                stats[recv_rid]["rcvd"] += val
            if send_rid:
                stats[send_rid]["sent"] += val

    team_by_rid = {r["roster_id"]: r["team"] for r in rosters}
    rows = []
    for rid, s in stats.items():
        if s["trades"] == 0:
            continue
        surplus = s["rcvd"] - s["sent"]
        if surplus < -5000:
            label = "Overpaying  <-- target"
        elif surplus < -2000:
            label = "Slight overpayer"
        elif surplus > 5000:
            label = "Getting value"
        elif surplus > 2000:
            label = "Slight value"
        else:
            label = "Fair"
        rows.append([
            team_by_rid.get(rid, str(rid)),
            s["trades"],
            f"{s['rcvd']:,}",
            f"{s['sent']:,}",
            f"{surplus:+,}",
            label,
        ])

    # Sort: biggest overpayers first (they're your best trade targets)
    rows.sort(key=lambda x: int(x[4].replace(",", "").replace("+", "")))
    return rows


def sell_high_buy_low(conn, rosters, trends, my_team_name):
    """
    Returns (sell_high, buy_low):
      sell_high: players on your roster trending up (>= +3%)
      buy_low:   players on other rosters trending down (<= -3%)
    """
    if not trends:
        return None, None

    my_roster = next(
        (r for r in rosters if my_team_name.lower() in r["team"].lower()), None
    )
    if not my_roster:
        return None, None

    my_pids = {p["player_id"] for p in my_roster["players"]}
    team_by_pid = {p["player_id"]: r["team"] for r in rosters for p in r["players"]}

    sell = sorted(
        [(pid, d) for pid, d in trends.items()
         if pid in my_pids and d["delta_pct"] >= 3 and d["position"] in POSITIONS],
        key=lambda x: x[1]["delta_pct"], reverse=True,
    )[:10]

    buy = sorted(
        [(pid, d) for pid, d in trends.items()
         if pid not in my_pids and pid in team_by_pid and d["delta_pct"] <= -3 and d["position"] in POSITIONS],
        key=lambda x: x[1]["delta_pct"],
    )[:10]

    return sell, buy, team_by_pid


def main():
    conn = db.get_connection()
    settings = load_settings(conn)
    fc_values = get_latest_fc_values(conn)
    rosters = get_rosters(conn, fc_values)
    pick_values = build_pick_value_table(fc_values)
    pick_assets = compute_pick_assets(conn, rosters, settings["draft_rounds"], pick_values)
    trends = get_value_trends(conn, days=7)
    tiers = classify_teams(rosters, settings["starting_slots"])

    print("=== Manager Trade Profile Analysis ===")
    print(f"Season: {settings['season']}  |  League: {len(rosters)} teams\n")

    # -----------------------------------------------------------------------
    # 1. Pick capital positions
    # -----------------------------------------------------------------------
    print("=== 1. Future Pick Capital Position ===")
    pick_rows = pick_capital_positions(conn, rosters, settings)
    print(tabulate(pick_rows, headers=["Team", "Acquired", "Gave Away", "Net", "Stance"], tablefmt="simple"))
    print()
    print("  Accumulating = future-focused; may resist giving more picks in trades.")
    print("  Depleting = present-focused; more open to receiving picks for players.")

    # -----------------------------------------------------------------------
    # 2. Team tier snapshot
    # -----------------------------------------------------------------------
    print("\n=== 2. Team Tiers ===")
    tier_rows = sorted(
        [[r["team"], tiers[r["roster_id"]]["tier"], f"{tiers[r['roster_id']]['score']:.0%}"] for r in rosters],
        key=lambda x: x[2], reverse=True,
    )
    print(tabulate(tier_rows, headers=["Team", "Tier", "Score"], tablefmt="simple"))

    # -----------------------------------------------------------------------
    # 3. Trade value analysis
    # -----------------------------------------------------------------------
    print("\n=== 3. Historical Trade Value Analysis ===")
    trade_rows = trade_value_analysis(conn, rosters, fc_values, pick_values)
    if trade_rows is None:
        print("  No transaction data. Run ingest.py to load trade history.")
    elif not trade_rows:
        print("  No completed trades found.")
    else:
        print(tabulate(
            trade_rows,
            headers=["Team", "Trades", "Total Rcvd", "Total Sent", "Net", "Tendency"],
            tablefmt="simple",
        ))
        print()
        print("  Key: Overpaying managers give away more than they receive -- they are your best trade targets.")
        print("  Trade them the side of a deal they historically overpay for.")

    # -----------------------------------------------------------------------
    # 4 & 5. Sell high / buy low
    # -----------------------------------------------------------------------
    result = sell_high_buy_low(conn, rosters, trends, MY_TEAM)

    if result[0] is None:
        print("\n=== 4. Sell High / Buy Low ===")
        print("  No trend data yet. Run ingest.py daily for at least 2 days.")
    else:
        sell, buy, team_by_pid = result

        print("\n=== 4. Sell High -- Your Players Trending Up ===")
        if sell:
            rows = [
                [d["name"], d["position"], f"{d['prev']:,}", f"{d['current']:,}", f"+{d['delta_pct']:.1f}%"]
                for _, d in sell
            ]
            print(tabulate(rows, headers=["Player", "Pos", "7d Ago", "Now", "Change"], tablefmt="simple"))
            print("  These players have rising market value -- ideal time to sell or ask for a premium.")
        else:
            print("  No significant risers on your roster this week.")

        print("\n=== 5. Buy Low -- Other Rosters Trending Down ===")
        if buy:
            rows = [
                [d["name"], d["position"], team_by_pid.get(pid, "?"),
                 f"{d['prev']:,}", f"{d['current']:,}", f"{d['delta_pct']:.1f}%"]
                for pid, d in buy
            ]
            print(tabulate(rows, headers=["Player", "Pos", "Owner", "7d Ago", "Now", "Change"], tablefmt="simple"))
            print("  These players have falling market value -- offer a slight discount to pry them away.")
        else:
            print("  No significant fallers on other rosters this week.")

    conn.close()


if __name__ == "__main__":
    main()
