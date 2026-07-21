"""
Usage: python trade_finder.py [team_name]

Identifies the best trade partners for a given team and suggests fair packages.
Defaults to MY_TEAM if no argument is provided.
Team name is matched case-insensitively as a substring of team/display names.
"""

import sys
from collections import defaultdict
from itertools import combinations

from tabulate import tabulate

import db
from utils import (
    POSITIONS,
    assign_starters,
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

MY_TEAM = "nboysko"

MIN_ASSET_VALUE = 500          # ignore players/picks below this value in packages
CONSOLIDATION_PREMIUM = 0.10   # 10% premium to the side holding fewer, concentrated assets
SECONDARY_ASSET_FLOOR = 0.20   # 2nd asset in a combo must be ≥ 20% of the top asset's value
PICK_PREMIUM_REBUILD = 0.20    # picks effectively worth 20% more to rebuilding teams
PLAYER_DISCOUNT_REBUILD = 0.10 # rebuilders willing to sell players at a 10% discount

TOLERANCE = 0.22
MAX_PARTNERS = 5
MAX_PACKAGES = 3

CONTEND_THRESHOLD = 0.60
REBUILD_THRESHOLD = 0.35


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
# Positional surplus / deficit scoring
# ---------------------------------------------------------------------------

def compute_positional_surplus(rosters, starting_slots):
    """
    Returns dict: roster_id -> {pos: surplus_value} for POSITIONS.
    Surplus = (my starter value at pos) - (league median starter value at pos).
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

    return {
        r["roster_id"]: {pos: slot_starter_vals[r["roster_id"]].get(pos, 0) - medians[pos] for pos in POSITIONS}
        for r in rosters
    }


# ---------------------------------------------------------------------------
# Trade partner ranking
# ---------------------------------------------------------------------------

def rank_trade_partners(my_roster, rosters, surplus):
    """
    Complementarity score = sum over POSITIONS of (-my_surplus[pos]) * their_surplus[pos].
    High score = they have what I lack and vice versa.
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


def find_rebuild_targets(my_roster, rosters, tiers):
    """
    Returns rebuilding/middle teams sorted by how rebuild-ready they are (lowest score first).
    These are sell candidates — offer them picks for their players.
    """
    my_rid = my_roster["roster_id"]
    targets = []
    for r in rosters:
        rid = r["roster_id"]
        if rid == my_rid:
            continue
        info = tiers[rid]
        if info["tier"] in ("Rebuilding", "Middle"):
            targets.append((info["score"], r))
    targets.sort(key=lambda x: x[0])
    return targets


# ---------------------------------------------------------------------------
# Package generation
# ---------------------------------------------------------------------------

def _is_fair(send_assets, recv_assets):
    """
    Value-balance check with consolidation premium.
    The side with fewer concentrated assets gets a 10% effective value boost,
    reflecting that a single elite player is worth more than its raw value
    when compared to a spread of equivalent value.
    """
    sv = sum(a["value"] for a in send_assets)
    rv = sum(a["value"] for a in recv_assets)

    if sv == 0 or rv == 0:
        return False

    n_s = len(send_assets)
    n_r = len(recv_assets)

    if n_s < n_r:
        sv *= (1 + CONSOLIDATION_PREMIUM)
    elif n_r < n_s:
        rv *= (1 + CONSOLIDATION_PREMIUM)

    ratio = sv / rv
    return (1 - TOLERANCE) <= ratio <= (1 + TOLERANCE)


def _all_picks(assets):
    return all(a["position"] == "PICK" for a in assets)


def _has_quality_depth(assets):
    """Every asset in a combo must be >= 20% of the highest-value asset in that combo."""
    if len(assets) <= 1:
        return True
    values = sorted([a["value"] for a in assets], reverse=True)
    return all(v >= values[0] * SECONDARY_ASSET_FLOOR for v in values[1:])


def generate_packages(send_assets, recv_assets, my_surplus):
    """
    Generates fair trade packages (1-for-1, 2-for-1, 1-for-2, 2-for-2).

    Filters:
    - Secondary asset floor: no asset in a combo may be < 20% of the combo's top asset
    - Trivial pick-for-pick swaps of equal value
    - Consolidation premium applied in value balance check

    Sorted by positional fit (receive deficit positions, send surplus positions),
    then by closeness to even value. Capped at MAX_PACKAGES.
    """
    results = []

    for s_size, r_size in [(1, 1), (2, 1), (1, 2), (2, 2)]:
        for s_combo in combinations(send_assets, s_size):
            if not _has_quality_depth(list(s_combo)):
                continue
            sv = sum(a["value"] for a in s_combo)
            if sv == 0:
                continue
            for r_combo in combinations(recv_assets, r_size):
                if not _has_quality_depth(list(r_combo)):
                    continue
                rv = sum(a["value"] for a in r_combo)
                if rv == 0:
                    continue
                if _all_picks(list(s_combo)) and _all_picks(list(r_combo)) and sv == rv:
                    continue
                if _is_fair(list(s_combo), list(r_combo)):
                    imbalance = abs(sv - rv) / max(sv, rv)
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


def generate_packages_seeded(seed_send, seed_recv, extra_send_pool, extra_recv_pool, my_surplus, max_total_per_side=2):
    """
    Like generate_packages, but seed_send/seed_recv are guaranteed to appear in
    every returned package. Fills in 0..N extra assets per side (from the pools,
    excluding anything already seeded) up to max_total_per_side assets on that side.
    If a seed list already meets/exceeds max_total_per_side, that side is used
    as-is with no extras searched. With no seeds at all, delegates to
    generate_packages for identical behavior to the unseeded search.
    """
    if not seed_send and not seed_recv:
        return generate_packages(extra_send_pool, extra_recv_pool, my_surplus)

    seed_send_ids = {a["player_id"] for a in seed_send}
    seed_recv_ids = {a["player_id"] for a in seed_recv}
    send_pool = [a for a in extra_send_pool if a["player_id"] not in seed_send_ids]
    recv_pool = [a for a in extra_recv_pool if a["player_id"] not in seed_recv_ids]

    max_extra_send = max(0, max_total_per_side - len(seed_send))
    max_extra_recv = max(0, max_total_per_side - len(seed_recv))

    results = []
    for n_extra_s in range(max_extra_send + 1):
        for extra_s in combinations(send_pool, n_extra_s):
            s_combo = list(seed_send) + list(extra_s)
            if not s_combo or not _has_quality_depth(s_combo):
                continue
            sv = sum(a["value"] for a in s_combo)
            if sv == 0:
                continue
            for n_extra_r in range(max_extra_recv + 1):
                for extra_r in combinations(recv_pool, n_extra_r):
                    r_combo = list(seed_recv) + list(extra_r)
                    if not r_combo or not _has_quality_depth(r_combo):
                        continue
                    rv = sum(a["value"] for a in r_combo)
                    if rv == 0:
                        continue
                    if _all_picks(s_combo) and _all_picks(r_combo) and sv == rv:
                        continue
                    if _is_fair(s_combo, r_combo):
                        imbalance = abs(sv - rv) / max(sv, rv)
                        fit = (
                            sum(-my_surplus.get(a["position"], 0) for a in r_combo if a["position"] in POSITIONS)
                            + sum(my_surplus.get(a["position"], 0) for a in s_combo if a["position"] in POSITIONS)
                        )
                        results.append((-fit, imbalance, s_combo, r_combo))

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
# Rebuild-context asset adjustment
# ---------------------------------------------------------------------------

def _rebuild_adjusted_assets(my_picks, their_players):
    """
    Returns asset lists with rebuild-context value adjustments:
    - My picks: inflated 20% (rebuilders prize future capital above face value)
    - Their players: discounted 10% (rebuilders are willing sellers)
    """
    adj_picks = [{**p, "value": int(p["value"] * (1 + PICK_PREMIUM_REBUILD))} for p in my_picks]
    adj_players = [{**p, "value": int(p["value"] * (1 - PLAYER_DISCOUNT_REBUILD))} for p in their_players]
    return adj_picks, adj_players


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


def _dynasty_warning(send_assets, recv_assets):
    """
    Warns when the prime-seasons math is significantly lopsided.
    RBs cliff at 27, WRs at 30 — same market value can hide a big future gap.
    Only fires when the difference is at least 3 seasons (signal, not noise).
    """
    send_prime = sum(prime_years_remaining(a) or 0 for a in send_assets if a["position"] in POSITIONS)
    recv_prime = sum(prime_years_remaining(a) or 0 for a in recv_assets if a["position"] in POSITIONS)

    if send_prime == 0 or recv_prime == 0:
        return None

    diff = send_prime - recv_prime
    if diff >= 3:
        return (
            f"Dynasty caution: ~{send_prime} prime seasons sent vs ~{recv_prime} received "
            f"({diff:+d}) -- market may be fair but you're trading future for present"
        )
    if diff <= -3:
        return (
            f"Dynasty edge: ~{recv_prime} prime seasons received vs ~{send_prime} sent "
            f"({diff:+d}) -- you gain future value beyond what the market reflects"
        )
    return None


def _trend_signals(assets, trends):
    """Returns list of trend signal strings for notable movers in a package."""
    signals = []
    for a in assets:
        d = trends.get(a["player_id"])
        if d and abs(d["delta_pct"]) >= 3 and a["position"] in POSITIONS:
            arrow = "+" if d["delta_pct"] > 0 else "-"
            signals.append(f"{a['full_name'].split()[1] if ' ' in a['full_name'] else a['full_name']} {arrow}{abs(d['delta_pct']):.1f}%")
    return signals


def _print_packages(packages, my_s, label, trends=None):
    print(f"\n  {label} (+/-{int(TOLERANCE * 100)}% tolerance, {int(CONSOLIDATION_PREMIUM * 100)}% consolidation premium):")
    for i, (send, recv) in enumerate(packages, 1):
        sv = sum(a["value"] for a in send)
        rv = sum(a["value"] for a in recv)
        notes = _pos_annotations(my_s, send, recv)
        note_str = "  ** " + "; ".join(notes) if notes else ""
        print(f"  {i}. YOU SEND: {_asset_str(send)} [{sv:,}]")
        print(f"     YOU GET:  {_asset_str(recv)} [{rv:,}]{note_str}")
        dynasty_warn = _dynasty_warning(send, recv)
        if dynasty_warn:
            print(f"     Dynasty:  {dynasty_warn}")
        if trends:
            sell_sigs = _trend_signals(send, trends)
            buy_sigs = _trend_signals(recv, trends)
            sig_parts = []
            if sell_sigs:
                sig_parts.append("Sell high: " + ", ".join(sell_sigs))
            if buy_sigs:
                sig_parts.append("Buy low: " + ", ".join(buy_sigs))
            if sig_parts:
                print(f"     Trend:    {' | '.join(sig_parts)}")


def print_report(my_roster, partners, surplus, pick_assets, tiers, rosters, trends=None):
    my_rid = my_roster["roster_id"]
    my_s = surplus[my_rid]
    my_tier = tiers[my_rid]

    record_tag = f" {my_tier['wins']}-{my_tier['losses']}" if my_tier.get("record_used") else ""
    print(f"\n=== Trade Finder: {my_roster['team']} [{my_tier['tier']}{record_tag}] ===")

    pos_rows = [[pos, f"{my_s[pos]:+,.0f}"] for pos in POSITIONS]
    print("\nYour positional surplus vs. league median (starters only):")
    print(tabulate(pos_rows, headers=["Pos", "Surplus"], tablefmt="simple"))

    my_players = filter_untouchables(
        [p for p in my_roster["players"] if p["position"] in POSITIONS and p["value"] >= MIN_ASSET_VALUE]
    )
    my_picks = [p for p in pick_assets.get(my_rid, []) if p["value"] >= MIN_ASSET_VALUE]
    my_assets = sorted(my_players + my_picks, key=lambda x: x["value"], reverse=True)

    # -----------------------------------------------------------------------
    # Section 1: Positional partners
    # -----------------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print("POSITIONAL TRADE PARTNERS")
    print(f"{'=' * 60}")

    for rank, (score, partner) in enumerate(partners[:MAX_PARTNERS], 1):
        prid = partner["roster_id"]
        their_s = surplus[prid]
        their_tier = tiers[prid]["tier"]

        print(f"\n--- #{rank}: {partner['team']} [{their_tier}] (fit score: {score:,.0f}) ---")

        their_pos_rows = [[pos, f"{their_s[pos]:+,.0f}"] for pos in POSITIONS]
        print(tabulate(their_pos_rows, headers=["Pos", "Surplus"], tablefmt="simple"))

        their_players = [p for p in partner["players"] if p["position"] in POSITIONS and p["value"] >= MIN_ASSET_VALUE]
        their_picks = [p for p in pick_assets.get(prid, []) if p["value"] >= MIN_ASSET_VALUE]
        their_assets = sorted(their_players + their_picks, key=lambda x: x["value"], reverse=True)

        packages = generate_packages(my_assets, their_assets, my_s)

        if not packages:
            print("  No fair packages found within value tolerance.")
            continue

        _print_packages(packages, my_s, "Suggested packages", trends)

    # -----------------------------------------------------------------------
    # Section 2: Rebuild targets
    # -----------------------------------------------------------------------
    rebuild_targets = find_rebuild_targets(my_roster, rosters, tiers)
    shown_rids = {partner["roster_id"] for _, partner in partners[:MAX_PARTNERS]}
    rebuild_targets = [(s, r) for s, r in rebuild_targets if r["roster_id"] not in shown_rids]

    if not rebuild_targets:
        return

    print(f"\n{'=' * 60}")
    print(f"REBUILD TARGETS  (picks boosted +{int(PICK_PREMIUM_REBUILD * 100)}%, their players discounted -{int(PLAYER_DISCOUNT_REBUILD * 100)}%)")
    print("These teams should be selling players for future capital.")
    print(f"{'=' * 60}")

    for contend_score, partner in rebuild_targets[:3]:
        prid = partner["roster_id"]
        their_tier = tiers[prid]["tier"]
        their_s = surplus[prid]

        print(f"\n--- {partner['team']} [{their_tier}] (contend score: {contend_score:.0%}) ---")

        their_pos_rows = [[pos, f"{their_s[pos]:+,.0f}"] for pos in POSITIONS]
        print(tabulate(their_pos_rows, headers=["Pos", "Surplus"], tablefmt="simple"))

        their_players = [p for p in partner["players"] if p["position"] in POSITIONS and p["value"] >= MIN_ASSET_VALUE]
        adj_picks, adj_their_players = _rebuild_adjusted_assets(my_picks, their_players)
        recv_assets = sorted(adj_their_players, key=lambda x: x["value"], reverse=True)

        # Try picks-only send first — that's the point of targeting rebuilders
        pick_send = sorted(adj_picks, key=lambda x: x["value"], reverse=True)
        packages = generate_packages(pick_send, recv_assets, my_s) if pick_send else []
        pick_only = bool(packages)

        # Fall back to picks + surplus players if no pick-only deals exist
        if not packages:
            mixed_send = sorted(my_players + adj_picks, key=lambda x: x["value"], reverse=True)
            packages = generate_packages(mixed_send, recv_assets, my_s)

        if not packages:
            print("  No fair packages found.")
            continue

        label = (
            f"Pick offers (picks boosted +{int(PICK_PREMIUM_REBUILD * 100)}%, their players discounted -{int(PLAYER_DISCOUNT_REBUILD * 100)}%)"
            if pick_only else
            f"Packages — no pick-only deals found; showing mixed (picks boosted +{int(PICK_PREMIUM_REBUILD * 100)}%)"
        )

        # Restore original face values for display
        display_packages = []
        for send, recv in packages:
            send_d = [
                {**a, "value": int(a["value"] / (1 + PICK_PREMIUM_REBUILD))} if a["position"] == "PICK" else a
                for a in send
            ]
            recv_d = [
                {**a, "value": int(a["value"] / (1 - PLAYER_DISCOUNT_REBUILD))} if a["position"] != "PICK" else a
                for a in recv
            ]
            display_packages.append((send_d, recv_d))

        _print_packages(display_packages, my_s, label, trends)

    # -----------------------------------------------------------------------
    # Section 3: Sell-side market (reverse trade finder)
    # -----------------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print("SELL-SIDE MARKET - What You Could Get for Each Asset")
    print("For each of your top players: who wants them, what's fair.")
    print(f"{'=' * 60}")

    my_sell_candidates = sorted(
        filter_untouchables(
            [p for p in my_roster["players"] if p["position"] in POSITIONS and p["value"] >= MIN_ASSET_VALUE]
        ),
        key=lambda x: x["value"], reverse=True,
    )[:8]

    for player in my_sell_candidates:
        pos = player["position"]

        trend_str = ""
        if trends:
            d = trends.get(player["player_id"])
            if d and abs(d["delta_pct"]) >= 3:
                arrow = "+" if d["delta_pct"] > 0 else "-"
                trend_str = f"  {arrow}{abs(d['delta_pct']):.1f}% (7d)"

        needy = sorted(
            [(surplus[r["roster_id"]].get(pos, 0), r)
             for r in rosters if r["roster_id"] != my_rid
             and surplus[r["roster_id"]].get(pos, 0) < 0],
            key=lambda x: x[0],
        )

        if not needy:
            print(f"\n  {player['full_name']} ({pos}, {player['value']:,}){trend_str} — no teams have a {pos} deficit")
            continue

        print(f"\n--- {player['full_name']} ({pos}, {player['value']:,}){trend_str} ---")
        print(f"  {len(needy)} team(s) short at {pos} and could want this player:\n")

        shown = 0
        for deficit, partner in needy[:4]:
            prid = partner["roster_id"]
            their_tier = tiers[prid]["tier"]
            their_players = [p for p in partner["players"] if p["position"] in POSITIONS and p["value"] >= MIN_ASSET_VALUE]
            their_picks = [p for p in pick_assets.get(prid, []) if p["value"] >= MIN_ASSET_VALUE]
            their_assets = sorted(their_players + their_picks, key=lambda x: x["value"], reverse=True)

            packages = generate_packages([player], their_assets, my_s)
            if not packages:
                continue

            shown += 1
            record_str = ""
            info = tiers[prid]
            if info.get("record_used"):
                record_str = f" {info['wins']}-{info['losses']}"
            print(f"  {partner['team']} [{their_tier}{record_str}] (their {pos} deficit: {deficit:+,.0f})")
            for send, recv in packages[:2]:
                rv = sum(a["value"] for a in recv)
                sv = sum(a["value"] for a in send)
                print(f"    YOU GET:  {_asset_str(recv)} [{rv:,}]")
                print(f"    YOU SEND: {_asset_str(send)} [{sv:,}]")
                if trends:
                    sigs = _trend_signals(recv, trends)
                    if sigs:
                        print(f"    Buy low:  {', '.join(sigs)}")

        if shown == 0:
            print(f"  Teams have {pos} deficit but can't offer fair value right now.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    team_arg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else MY_TEAM

    db.init_db()
    conn = db.get_connection()
    settings = load_settings(conn)
    fc_values = get_latest_fc_values(conn)
    rosters = get_rosters(conn, fc_values)
    pick_values = build_pick_value_table(fc_values)

    my_roster = find_my_roster(rosters, team_arg)
    pick_assets = compute_pick_assets(conn, rosters, settings["draft_rounds"], pick_values)
    surplus = compute_positional_surplus(rosters, settings["starting_slots"])
    tiers = classify_teams(rosters, settings["starting_slots"])
    partners = rank_trade_partners(my_roster, rosters, surplus)

    trends = get_value_trends(conn, days=7)
    if trends:
        print(f"  (Value trend data available: {len(trends)} movers in the past 7 days)")

    print_report(my_roster, partners, surplus, pick_assets, tiers, rosters, trends)

    conn.close()


if __name__ == "__main__":
    main()
