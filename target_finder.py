"""
Usage: python target_finder.py <player name>

Given a player you want to acquire, shows fair packages from your roster to get them.
Player name matched case-insensitively (substring ok).

Examples:
  python target_finder.py bijan
  python target_finder.py "breece hall"
  python target_finder.py gibbs
"""

import sys

from tabulate import tabulate

import db
from utils import (
    POSITIONS,
    PRIME_CLIFF_AGE,
    build_pick_value_table,
    classify_teams,
    compute_pick_assets,
    filter_untouchables,
    get_latest_fc_values,
    get_rosters,
    get_value_trends,
    load_settings,
    prime_years_remaining,
)
from trade_finder import (
    MIN_ASSET_VALUE,
    MY_TEAM,
    PICK_PREMIUM_REBUILD,
    _asset_str,
    _pos_annotations,
    _print_packages,
    _trend_signals,
    compute_positional_surplus,
    find_my_roster,
    generate_packages,
)


def find_target_player(name_query, rosters):
    """
    Returns (player_dict, owner_roster) for the best name match.
    Raises SystemExit if no match or ambiguous.
    """
    needle = name_query.lower()
    matches = []
    for r in rosters:
        for p in r["players"]:
            if needle in p["full_name"].lower():
                matches.append((p, r))

    if not matches:
        raise SystemExit(f"No player found matching '{name_query}'.\nTry a last name or more of the full name.")

    if len(matches) == 1:
        return matches[0]

    # Prefer exact start-of-name match to resolve common cases (e.g. "bijan" → Bijan Robinson)
    starts = [(p, r) for p, r in matches if p["full_name"].lower().startswith(needle)]
    if len(starts) == 1:
        return starts[0]

    # Still ambiguous
    options = "\n  ".join(
        f"{p['full_name']} ({p['position']}, {p['value']:,}) — {r['team']}"
        for p, r in matches[:10]
    )
    raise SystemExit(f"Multiple matches for '{name_query}':\n  {options}\n\nBe more specific.")


def main():
    if len(sys.argv) < 2:
        raise SystemExit(
            "Usage: python target_finder.py <player name>\n"
            "  Example: python target_finder.py bijan"
        )

    name_query = " ".join(sys.argv[1:])

    db.init_db()
    conn = db.get_connection()
    settings = load_settings(conn)
    fc_values = get_latest_fc_values(conn)
    rosters = get_rosters(conn, fc_values)
    pick_values = build_pick_value_table(fc_values)

    my_roster = find_my_roster(rosters, MY_TEAM)
    pick_assets = compute_pick_assets(conn, rosters, settings["draft_rounds"], pick_values)
    surplus = compute_positional_surplus(rosters, settings["starting_slots"])
    tiers = classify_teams(rosters, settings["starting_slots"])
    trends = get_value_trends(conn, days=7)

    target, target_owner = find_target_player(name_query, rosters)

    if target_owner["roster_id"] == my_roster["roster_id"]:
        raise SystemExit(f"{target['full_name']} is already on your roster.")

    my_rid = my_roster["roster_id"]
    my_s = surplus[my_rid]
    target_rid = target_owner["roster_id"]
    target_tier = tiers[target_rid]
    pos = target["position"]

    # -----------------------------------------------------------------------
    # Build send pool
    # -----------------------------------------------------------------------
    my_players = filter_untouchables([
        p for p in my_roster["players"]
        if p["position"] in POSITIONS and p["value"] >= MIN_ASSET_VALUE
    ])
    my_picks_raw = [p for p in pick_assets.get(my_rid, []) if p["value"] >= MIN_ASSET_VALUE]

    # Inflate picks when the target owner is rebuilding — they prize future capital
    if target_tier["tier"] == "Rebuilding":
        my_picks = [{**p, "value": int(p["value"] * (1 + PICK_PREMIUM_REBUILD))} for p in my_picks_raw]
        pick_note = f"  (your picks boosted +{int(PICK_PREMIUM_REBUILD * 100)}% -- they're rebuilding)"
    else:
        my_picks = my_picks_raw
        pick_note = ""

    send_pool = sorted(my_players + my_picks, key=lambda x: x["value"], reverse=True)

    # -----------------------------------------------------------------------
    # Header
    # -----------------------------------------------------------------------
    trend_str = ""
    if trends:
        d = trends.get(target["player_id"])
        if d and abs(d["delta_pct"]) >= 3:
            direction = "up" if d["delta_pct"] > 0 else "down"
            context = "owner may want to sell high" if d["delta_pct"] > 0 else "buy low opportunity"
            trend_str = f"  [{direction} {abs(d['delta_pct']):.1f}% this week — {context}]"

    record_str = ""
    if target_tier.get("record_used"):
        record_str = f" {target_tier['wins']}-{target_tier['losses']}"

    print(f"\n=== Target: {target['full_name']} ({pos}, {target['value']:,}){trend_str} ===")
    print(f"Owner: {target_owner['team']} [{target_tier['tier']}{record_str}]{pick_note}")

    # -----------------------------------------------------------------------
    # Positional context
    # -----------------------------------------------------------------------
    my_deficit = my_s.get(pos, 0)
    if my_deficit < 0:
        print(f"\nYour {pos} surplus: {my_deficit:+,.0f}  <- you need this position")
    else:
        print(f"\nYour {pos} surplus: {my_deficit:+,.0f}  <- you already have depth here")

    their_s = surplus[target_rid]
    surplus_rows = [[p, f"{their_s[p]:+,.0f}"] for p in POSITIONS]
    print(f"\n{target_owner['team']}'s positional surplus (their surpluses = trade bait):")
    print(tabulate(surplus_rows, headers=["Pos", "Surplus"], tablefmt="simple"))

    # -----------------------------------------------------------------------
    # Generate packages
    # -----------------------------------------------------------------------
    # target is the single asset we want to receive; generate_packages finds
    # what combinations from send_pool match its value (1-for-1 and 2-for-1)
    packages = generate_packages(send_pool, [target], my_s)

    # Restore pick face values for display if we inflated them
    if target_tier["tier"] == "Rebuilding":
        display_packages = []
        for send, recv in packages:
            send_display = [
                {**a, "value": int(a["value"] / (1 + PICK_PREMIUM_REBUILD))}
                if a["position"] == "PICK" else a
                for a in send
            ]
            display_packages.append((send_display, recv))
    else:
        display_packages = packages

    # -----------------------------------------------------------------------
    # Dynasty context for the target
    # -----------------------------------------------------------------------
    target_prime = prime_years_remaining(target)
    if target_prime is not None:
        cliff = PRIME_CLIFF_AGE.get(pos, "?")
        age_str = f"age {int(target['age'])}" if target.get("age") else "age unknown"
        print(f"\nDynasty window: {target['full_name']} ({pos}, {age_str}) -- ~{target_prime} prime seasons remaining (cliff: age {cliff})")
        if pos == "RB" and target_prime <= 3:
            print("  Warning: this RB is approaching their production cliff. Consider a shorter commitment.")
        elif pos == "RB" and target_prime <= 5:
            print("  Note: RBs cliff ~4 years earlier than WRs. Factor this into how much future value you give up.")

    if not display_packages:
        print(f"\nNo fair packages found.")
        print(
            f"{target['full_name']} is valued at {target['value']:,}. "
            f"Your tradeable assets may not match — consider including picks."
        )
        # Show your top assets so the user can reason about it
        print("\nYour top tradeable assets:")
        asset_rows = [
            [p["full_name"], p["position"], f"{p['value']:,}"]
            for p in send_pool[:8]
        ]
        print(tabulate(asset_rows, headers=["Player/Pick", "Pos", "Value"], tablefmt="simple"))
    else:
        label = f"What to offer for {target['full_name']}"
        _print_packages(display_packages, my_s, label, trends)

    conn.close()


if __name__ == "__main__":
    main()
