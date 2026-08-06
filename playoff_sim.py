"""
Playoff odds — Monte Carlo simulation of the remaining regular season.

Uses the real schedule (Sleeper pre-generates all regular-season matchups
before they're played) plus a per-team strength distribution to simulate
the rest of the season thousands of times and report what fraction of the
time each team finishes inside the playoff bracket.

Team strength starts as a value-based prior (works even preseason, when
there's no real fpts history yet) and blends toward each team's own
empirical weekly scoring as real weeks accumulate -- by ~SHRINK_FULL_WEEKS
weeks in, the prior's influence is gone and the model runs on actual
performance.

v1 scope: a standalone odds view. Surfacing "how much does trading for
Player X move my playoff odds" inside the trade builder is an intentional
fast-follow, not implemented here (see CLAUDE.md roadmap).
"""
import random
import statistics
from collections import Counter, defaultdict

import utils

N_SIMULATIONS = 10000
LEAGUE_AVG_WEEKLY_POINTS = 130.0   # rough superflex / 1PPR / 0.5 TE-premium prior
SIGMA_PRIOR_PCT = 0.16             # flat weekly SD as % of league avg (preseason)
SHRINK_FULL_WEEKS = 6              # weeks of real data until fully trusting empirical mu/sigma
MIN_WEEKS_FOR_EMPIRICAL_SIGMA = 3

TIEBREAKER_NOTE = (
    "Standings approximated as win% then points-for; this league's real Sleeper "
    "tiebreaker chain (divisions, head-to-head, etc.) is not modeled."
)


def _starter_values(rosters, starting_slots):
    """Starter (not total-roster) FC value per roster_id -- the same signal
    classify_teams() uses for its starter-rank score, computed directly here
    to avoid re-deriving it from classify_teams' blended 0-1 score."""
    vals = {}
    for r in rosters:
        active = [p for p in r["players"] if p["slot"] == "active"]
        starters, _ = utils.assign_starters(active, starting_slots)
        vals[r["roster_id"]] = sum(p["value"] for p in starters)
    return vals


def load_matchup_rows(conn, season):
    return conn.execute(
        "SELECT week, roster_id, matchup_id, points FROM matchups"
        " WHERE season = ? ORDER BY week",
        (season,),
    ).fetchall()


def build_schedule(matchup_rows, playoff_week_start):
    """Returns {week: [(roster_a, roster_b, points_a, points_b), ...]},
    regular-season weeks only. Two matchup rows sharing (week, matchup_id)
    are one head-to-head pairing."""
    by_week_matchup = defaultdict(list)
    for row in matchup_rows:
        if row["week"] >= playoff_week_start:
            continue
        by_week_matchup[(row["week"], row["matchup_id"])].append(row)

    schedule = defaultdict(list)
    for (week, _matchup_id), entries in by_week_matchup.items():
        if len(entries) != 2:
            continue  # bye or malformed pairing -- skip rather than guess
        a, b = entries
        schedule[week].append((a["roster_id"], b["roster_id"], a["points"], b["points"]))
    return dict(schedule)


def estimate_team_distributions(rosters, starting_slots, schedule, current_week):
    """Returns {roster_id: (mu, sigma)} -- each team's per-week scoring
    distribution, blending a value-based prior with empirical in-season
    performance as more real weeks accumulate."""
    starter_vals = _starter_values(rosters, starting_slots)
    avg_starter_val = statistics.mean(starter_vals.values()) if starter_vals else 0.0
    avg_starter_val = avg_starter_val or 1.0  # avoid div-by-zero if values are missing

    played_points = defaultdict(list)
    for week, pairs in schedule.items():
        if week >= current_week:
            continue
        for ra, rb, pa, pb in pairs:
            played_points[ra].append(pa)
            played_points[rb].append(pb)

    weeks_played = max(0, current_week - 1)
    shrink_w = min(1.0, weeks_played / SHRINK_FULL_WEEKS)

    dist = {}
    for r in rosters:
        rid = r["roster_id"]
        mu_prior = LEAGUE_AVG_WEEKLY_POINTS * (starter_vals.get(rid, 0) / avg_starter_val)
        sigma_prior = LEAGUE_AVG_WEEKLY_POINTS * SIGMA_PRIOR_PCT

        pts = played_points.get(rid, [])
        if len(pts) >= MIN_WEEKS_FOR_EMPIRICAL_SIGMA:
            mu_emp = statistics.mean(pts)
            sigma_emp = statistics.stdev(pts) if len(pts) > 1 else sigma_prior
            mu = shrink_w * mu_emp + (1 - shrink_w) * mu_prior
            sigma = shrink_w * sigma_emp + (1 - shrink_w) * sigma_prior
        else:
            mu, sigma = mu_prior, sigma_prior

        dist[rid] = (max(mu, 1.0), max(sigma, 1.0))
    return dist


def run_monte_carlo(rosters, schedule, dist_by_roster, playoff_teams, current_week,
                     n_sims=N_SIMULATIONS):
    roster_ids = [r["roster_id"] for r in rosters]
    base_wins = {r["roster_id"]: r["wins"] for r in rosters}
    base_losses = {r["roster_id"]: r["losses"] for r in rosters}
    base_ties = {r["roster_id"]: r["ties"] for r in rosters}
    # rosters[i]["fpts"] is Sleeper's own season-to-date total -- already
    # includes every played week, so future-week samples get added on top
    # rather than re-summing weeks we already have real points for.
    base_pf = {r["roster_id"]: r.get("fpts", 0) or 0.0 for r in rosters}

    future_weeks = [w for w in schedule if w >= current_week]

    playoff_count = Counter()
    sum_wins = defaultdict(float)
    sum_pf = defaultdict(float)

    for _ in range(n_sims):
        wins = dict(base_wins)
        losses = dict(base_losses)
        ties = dict(base_ties)
        pf = dict(base_pf)

        for week in future_weeks:
            for ra, rb, _pa, _pb in schedule[week]:
                mu_a, sig_a = dist_by_roster[ra]
                mu_b, sig_b = dist_by_roster[rb]
                score_a = max(0.0, random.gauss(mu_a, sig_a))
                score_b = max(0.0, random.gauss(mu_b, sig_b))
                pf[ra] += score_a
                pf[rb] += score_b
                if score_a > score_b:
                    wins[ra] += 1
                    losses[rb] += 1
                elif score_b > score_a:
                    wins[rb] += 1
                    losses[ra] += 1
                else:
                    ties[ra] += 1
                    ties[rb] += 1

        standings = sorted(
            roster_ids,
            key=lambda rid: (
                -(wins[rid] + 0.5 * ties[rid]) / max(1, wins[rid] + losses[rid] + ties[rid]),
                -pf[rid],
            ),
        )
        for rid in standings[:playoff_teams]:
            playoff_count[rid] += 1
        for rid in roster_ids:
            sum_wins[rid] += wins[rid]
            sum_pf[rid] += pf[rid]

    return {
        rid: {
            "playoff_pct": playoff_count[rid] / n_sims * 100,
            "avg_final_wins": sum_wins[rid] / n_sims,
            "avg_points_for": sum_pf[rid] / n_sims,
        }
        for rid in roster_ids
    }


def compute_playoff_odds(conn, rosters, settings):
    season = settings["season"]
    playoff_week_start = settings["playoff_week_start"]
    playoff_teams = settings["playoff_teams"]
    current_week = settings["current_week"]
    starting_slots = settings["starting_slots"]
    weeks_played = max(0, current_week - 1)

    matchup_rows = load_matchup_rows(conn, season)
    schedule = build_schedule(matchup_rows, playoff_week_start)
    if not schedule:
        return {
            "current_week": current_week,
            "playoff_teams": playoff_teams,
            "weeks_played": weeks_played,
            "tiebreaker_note": TIEBREAKER_NOTE,
            "teams": [],
            "error": "No matchup schedule found -- run ingest.py first.",
        }

    dist = estimate_team_distributions(rosters, starting_slots, schedule, current_week)
    sim_result = run_monte_carlo(rosters, schedule, dist, playoff_teams, current_week)

    by_roster_id = {r["roster_id"]: r for r in rosters}
    teams = []
    for rid, stats in sim_result.items():
        r = by_roster_id[rid]
        teams.append({
            "roster_id": rid,
            "team": r["team"],
            "wins": r["wins"],
            "losses": r["losses"],
            "ties": r["ties"],
            "playoff_pct": stats["playoff_pct"],
            "avg_final_wins": stats["avg_final_wins"],
            "avg_points_for": stats["avg_points_for"],
        })
    teams.sort(key=lambda t: -t["playoff_pct"])

    return {
        "current_week": current_week,
        "playoff_teams": playoff_teams,
        "weeks_played": weeks_played,
        "tiebreaker_note": TIEBREAKER_NOTE,
        "teams": teams,
    }


if __name__ == "__main__":
    import db
    from tabulate import tabulate

    db.init_db()
    conn = db.get_connection()
    settings = utils.load_settings(conn)
    fc_values = utils.get_latest_fc_values(conn)
    rosters = utils.get_rosters(conn, fc_values)
    conn.close()

    data = compute_playoff_odds(db.get_connection(), rosters, settings)
    print(f"Week {data['current_week']} ({data['weeks_played']} played) -- "
          f"top {data['playoff_teams']} of {len(data.get('teams', []))} make playoffs")
    print(f"Note: {data['tiebreaker_note']}\n")
    rows = [
        [t["team"], f"{t['wins']}-{t['losses']}-{t['ties']}", f"{t['playoff_pct']:.1f}%",
         f"{t['avg_final_wins']:.1f}", f"{t['avg_points_for']:.0f}"]
        for t in data["teams"]
    ]
    print(tabulate(rows, headers=["Team", "Record", "Playoff %", "Avg Final Wins", "Avg PF"]))
