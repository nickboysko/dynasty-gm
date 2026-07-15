"""
Usage: python trade_finder.py <team_name>

Identifies the best trade partners for a given team and suggests fair packages.
Team name is matched case-insensitively as a substring of team/display names.
"""

import sys
from collections import defaultdict
from datetime import date
from itertools import combinations

from tabulate import tabulate

import db
from utils import (
    POSITIONS,
    assign_starters,
    build_pick_value_table,
    get_latest_fc_values,
    get_rosters,
    load_settings,
)

TOLERANCE = 0.22   # max value imbalance considered "fair" (22%)
MAX_PARTNERS = 5   # top trade partners to display
MAX_PACKAGES = 3   # trade packages to show per partner


# ---------------------------------------------------------------------------
# Roster lookup
# ---------------------------------------------------------------------------

def find_my_roster(rosters, team_arg):
    needle = team_arg.lower()
    matches = [r for r in rosters if needle in r["team"].lower()]
    if not matches:
        names = ", ".join(r["team"] for r in rosters)
        raise SystemExit(f"No team matched '{team_arg}'. Available teams:\n  {names}")
    if len(matches) > 1:
        names = ", ".join(r["team"] for r in matches)
        raise SystemExit(f"Ambiguous match for '{team_arg}'. Matched:\n  {names}")
    return matches[0]


# ---------------------------------------------------------------------------
# Pick capital as tradeable assets
# ---------------------------------------------------------------------------

def compute_pick_assets(conn, rosters, draft_rounds, pick_values):
    """
    Returns dict: roster_id -> list of pick asset dicts.
    Each pick asset looks like a player with position='PICK'.
    """
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

    traded = conn.execute(
        "SELECT season, round, original_roster_id, current_roster_id FROM traded_picks"
    ).fetchall()
    for t in traded:
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


# ---------------------------------------------------------------------------
# Positional surplus / deficit scoring
# ---------------------------------------------------------------------------

def compute_positional_surplus(rosters, starting_slots):
    """
    Returns dict: roster_id -> {pos: surplus_value} for POSITIONS.

    Surplus = (my starter value at pos) - (league median starter value at pos).
    Positive = strength; negative = weakness.
    Only skill positions (QB/RB/WR/TE) are scored.
    """
    slot_starter_vals = {}
    for r in rosters:
        active = [p for p in r["players"] if p["slot"] == "active"]
        starters, _ = assign_starters(active, starting_slots)
        pos_val = defaultdict(int)
        for p in starters:
            if p["position"] in POSITIONS:
                pos_val[p["position"]] += p["value"]
        slot_starter_vals[r["roster_id"]] = pos_val

    medians = {}
    for pos in POSITIONS:
        vals = sorted(slot_starter_vals[rid].get(pos, 0) for rid in slot_starter_vals)
        n = len(vals)
        medians[pos] = (vals[n // 2 - 1] + vals[n // 2]) / 2 if n % 2 == 0 else vals[n // 2]

    surplus = {}
    for r in rosters:
        rid = r["roster_id"]
        surplus[rid] = {pos: slot_starter_vals[rid].get(pos, 0) - medians[pos] for pos in POSITIONS}
    return surplus


# ---------------------------------------------------------------------------
# Trade partner ranking
# ---------------------------------------------------------------------------

def rank_trade_partners(my_roster, rosters, surplus):
    """
    Score = sum over POSITIONS of (-my_surplus[pos]) * their_surplus[pos].
    High score = they have what I lack and I have what they lack.
    """
    my_rid = my_roster["roster_id"]
    my_s = surplus[my_rid]
    scores = []
    for r in rosters:
        if r["roster_id"] == my_rid:
            continue
        their_s = surplus[r["roster_id"]]
        score = sum((-my_s[pos]) * their_s[pos] for pos in POSITIONS)
        scores.append((score, r))
    scores.sort(key=lambda x: x[0], reverse=True)
    return scores


# ---------------------------------------------------------------------------
# Package generation
# ---------------------------------------------------------------------------

def _is_fair(send_val, recv_val):
    if send_val == 0 and recv_val == 0:
        return True
    if send_val == 0 or recv_val == 0:
        return False
    ratio = send_val / recv_val
    return (1 - TOLERANCE) <= ratio <= (1 + TOLERANCE)


def _all_picks(assets):
    return all(a["position"] == "PICK" for a in assets)


def generate_packages(send_assets, recv_assets, my_surplus):
    """
    Try 1-for-1, 2-for-1, 1-for-2, and 2-for-2 combinations.
    Filters trivial all-pick equal-value swaps (pointless same-round exchanges).
    Sorts by:
      1. Positional fit: reward receiving deficit positions, sending surplus positions
      2. Then by closeness to even value
    Capped at MAX_PACKAGES.
    """
    results = []

    for s_size, r_size in [(1, 1), (2, 1), (1, 2), (2, 2)]:
        for s_combo in combinations(send_assets, s_size):
            sv = sum(a["value"] for a in s_combo)
            if sv == 0:
                continue
            for r_combo in combinations(recv_assets, r_size):
                rv = sum(a["value"] for a in r_combo)
                if rv == 0:
                    continue
                # Skip trivial pick-for-pick swaps with equal value
                if _all_picks(list(s_combo)) and _all_picks(list(r_combo)) and sv == rv:
                    continue
                if _is_fair(sv, rv):
                    imbalance = abs(sv - rv) / max(sv, rv)
                    # Fit score: receive what I lack, send what I have extra
                    fit = (
                        sum(-my_surplus.get(a["position"], 0) for a in r_combo if a["position"] in POSITIONS)
                        + sum(my_surplus.get(a["position"], 0) for a in s_combo if a["position"] in POSITIONS)
                    )
                    results.append((-fit, imbalance, list(s_combo), list(r_combo)))

    results.sort(key=lambda x: (x[0], x[1]))

    seen = set()
    unique = []
    for _, _, s, r in results:
        key = (
            frozenset(a["player_id"] for a in s),
            frozenset(a["player_id"] for a in r),
        )
        if key not in seen:
            seen.add(key)
            unique.append((s, r))
        if len(unique) >= MAX_PACKAGES:
            break

    return unique


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

def _pos_annotations(my_surplus, send_assets, recv_assets):
    notes = []
    send_pos = [a["position"] for a in send_assets if a["position"] in POSITIONS]
    recv_pos = [a["position"] for a in recv_assets if a["position"] in POSITIONS]

    for pos in set(recv_pos):
        if my_surplus.get(pos, 0) < 0:
            notes.append(f"Addresses your {pos} deficit")

    for pos in set(send_pos):
        if my_surplus.get(pos, 0) < 0:
            notes.append(f"Caution: sending {pos} despite deficit")

    return notes


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _asset_str(assets):
    return " + ".join(f"{a['full_name']} ({a['position']}, {a['value']:,})" for a in assets)


def print_report(my_roster, partners, surplus, pick_assets):
    my_rid = my_roster["roster_id"]
    my_s = surplus[my_rid]

    print(f"\n=== Trade Finder: {my_roster['team']} ===")

    pos_rows = [[pos, f"{my_s[pos]:+,.0f}"] for pos in POSITIONS]
    print("\nYour positional surplus vs. league median (starters only):")
    print(tabulate(pos_rows, headers=["Pos", "Surplus"], tablefmt="simple"))

    if not partners:
        print("\nNo complementary trade partners found.")
        return

    my_players = [p for p in my_roster["players"] if p["position"] in POSITIONS and p["value"] > 0]
    my_picks = pick_assets.get(my_rid, [])
    my_assets = sorted(my_players + my_picks, key=lambda x: x["value"], reverse=True)

    for rank, (score, partner) in enumerate(partners[:MAX_PARTNERS], 1):
        prid = partner["roster_id"]
        their_s = surplus[prid]

        print(f"\n--- Partner #{rank}: {partner['team']} (complementarity score: {score:,.0f}) ---")

        their_pos_rows = [[pos, f"{their_s[pos]:+,.0f}"] for pos in POSITIONS]
        print(tabulate(their_pos_rows, headers=["Pos", "Surplus"], tablefmt="simple"))

        their_players = [p for p in partner["players"] if p["position"] in POSITIONS and p["value"] > 0]
        their_picks = pick_assets.get(prid, [])
        their_assets = sorted(their_players + their_picks, key=lambda x: x["value"], reverse=True)

        packages = generate_packages(my_assets, their_assets, my_s)

        if not packages:
            print("  No fair packages found within value tolerance.")
            continue

        print(f"\n  Suggested packages (tolerance ±{int(TOLERANCE * 100)}%):")
        for i, (send, recv) in enumerate(packages, 1):
            sv = sum(a["value"] for a in send)
            rv = sum(a["value"] for a in recv)
            notes = _pos_annotations(my_s, send, recv)
            note_str = "  ** " + "; ".join(notes) if notes else ""
            print(f"  {i}. YOU SEND: {_asset_str(send)} [{sv:,}]")
            print(f"     YOU GET:  {_asset_str(recv)} [{rv:,}]{note_str}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python trade_finder.py <team_name>")

    team_arg = " ".join(sys.argv[1:])

    conn = db.get_connection()
    settings = load_settings(conn)
    fc_values = get_latest_fc_values(conn)
    rosters = get_rosters(conn, fc_values)
    pick_values = build_pick_value_table(fc_values)

    my_roster = find_my_roster(rosters, team_arg)
    pick_assets = compute_pick_assets(conn, rosters, settings["draft_rounds"], pick_values)
    surplus = compute_positional_surplus(rosters, settings["starting_slots"])
    partners = rank_trade_partners(my_roster, rosters, surplus)

    print_report(my_roster, partners, surplus, pick_assets)

    conn.close()


if __name__ == "__main__":
    main()
