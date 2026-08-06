import json
import time
from datetime import datetime, timezone

import db
import fantasycalc
import sleeper

LEAGUE_ID = "1312107851514155008"
NON_STARTING_SLOTS = {"BN", "IR", "TAXI"}
SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}


# ---------------------------------------------------------------------------
# League format detection
# ---------------------------------------------------------------------------

def derive_league_format(league):
    roster_positions = league["roster_positions"]
    scoring = league.get("scoring_settings", {})
    settings = league.get("settings", {})

    superflex = "SUPER_FLEX" in roster_positions
    num_qbs = 2 if superflex else 1
    ppr = scoring.get("rec", 0)
    te_premium = scoring.get("bonus_rec_te", 0)
    starting_slots = [p for p in roster_positions if p not in NON_STARTING_SLOTS]
    draft_rounds = settings.get("draft_rounds", 5)
    season = league.get("season", str(datetime.now(timezone.utc).year))
    playoff_teams = settings.get("playoff_teams", 6)
    playoff_week_start = settings.get("playoff_week_start", 15)

    return {
        "superflex": superflex,
        "num_qbs": num_qbs,
        "ppr": ppr,
        "te_premium": te_premium,
        "starting_slots": starting_slots,
        "roster_positions": roster_positions,
        "draft_rounds": draft_rounds,
        "season": season,
        "playoff_teams": playoff_teams,
        "playoff_week_start": playoff_week_start,
    }


def derive_current_week(nfl_state, playoff_week_start):
    """How many regular-season weeks are 'in the books' league-wide.

    Sleeper's /state/nfl reports week=0 pre-season (points==0 everywhere is
    ambiguous with a real shutout, so we need this rather than inferring from
    points). Regular season uses the reported week directly; once the real
    NFL season has moved into the postseason, treat the whole regular season
    as played.
    """
    season_type = nfl_state.get("season_type")
    if season_type == "regular":
        return max(1, nfl_state.get("week", 1))
    if season_type in ("post", "complete"):
        return playoff_week_start
    return 1  # pre-season / off-season: nothing played yet


# ---------------------------------------------------------------------------
# Ingest helpers
# ---------------------------------------------------------------------------

def ingest_players(conn, players_data):
    rows = []
    for pid, p in players_data.items():
        if p.get("position") not in SKILL_POSITIONS:
            continue
        full_name = p.get("full_name") or (
            f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        )
        rows.append((
            pid, full_name, p.get("position"), p.get("team"), p.get("age"), p.get("years_exp"),
            p.get("status"), p.get("injury_status"), p.get("injury_body_part"),
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO players"
        " (player_id, full_name, position, team, age, years_exp, status, injury_status, injury_body_part)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def ingest_users(conn, users_data):
    rows = [
        (
            u["user_id"],
            u.get("display_name"),
            u.get("metadata", {}).get("team_name") or u.get("display_name"),
        )
        for u in users_data
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO users (user_id, display_name, team_name) VALUES (?, ?, ?)",
        rows,
    )


def ingest_rosters(conn, rosters_data):
    # Rebuild roster_players from scratch each run — roster moves happen frequently
    conn.execute("DELETE FROM roster_players")

    roster_rows = []
    for r in rosters_data:
        s = r.get("settings") or {}
        roster_rows.append((
            r["roster_id"],
            r.get("owner_id"),
            s.get("wins", 0),
            s.get("losses", 0),
            s.get("ties", 0),
            s.get("fpts", 0) or 0,
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO rosters (roster_id, owner_id, wins, losses, ties, fpts)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        roster_rows,
    )

    player_rows = []
    for r in rosters_data:
        rid = r["roster_id"]
        reserve = set(r.get("reserve") or [])
        taxi = set(r.get("taxi") or [])
        for pid in r.get("players") or []:
            if pid in reserve:
                slot = "ir"
            elif pid in taxi:
                slot = "taxi"
            else:
                slot = "active"
            player_rows.append((rid, pid, slot))
    conn.executemany(
        "INSERT OR REPLACE INTO roster_players (roster_id, player_id, slot) VALUES (?, ?, ?)",
        player_rows,
    )


def ingest_traded_picks(conn, picks_data):
    # Rebuild each run — trade ownership changes.
    # Sleeper: roster_id = original team, owner_id = current holder.
    conn.execute("DELETE FROM traded_picks")
    rows = [
        (
            p["season"],
            p["round"],
            p["roster_id"],                       # original_roster_id
            p.get("owner_id") or p["roster_id"],  # current_roster_id
        )
        for p in picks_data
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO traded_picks (season, round, original_roster_id, current_roster_id)"
        " VALUES (?, ?, ?, ?)",
        rows,
    )


def ingest_transactions(conn, season, transactions_by_week):
    """Insert or replace all completed transactions across all fetched weeks."""
    rows = []
    for week, txns in transactions_by_week.items():
        for t in txns:
            if not t.get("transaction_id"):
                continue
            rows.append((
                t["transaction_id"],
                t.get("type"),
                t.get("status"),
                season,
                week,
                json.dumps(t.get("roster_ids") or []),
                json.dumps(t.get("adds") or {}),
                json.dumps(t.get("drops") or {}),
                json.dumps(t.get("draft_picks") or []),
                t.get("created"),
            ))
    conn.executemany(
        "INSERT OR REPLACE INTO transactions"
        " (transaction_id, type, status, season, week, roster_ids, adds, drops, draft_picks, created)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def ingest_fc_values(conn, fc_data, fetched_at):
    rows = [
        (
            item["player"]["sleeperId"],
            item["player"].get("name"),
            item["player"].get("position"),
            item["value"],
            fetched_at,
        )
        for item in fc_data
        if item.get("player", {}).get("sleeperId")
    ]
    conn.executemany(
        "INSERT INTO fc_values (player_id, name, position, value, fetched_at) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def ingest_matchups(conn, season, matchups_by_week):
    rows = []
    for week, entries in matchups_by_week.items():
        for m in entries:
            rows.append((
                season,
                week,
                m["roster_id"],
                m.get("matchup_id"),
                m.get("points") or 0.0,
            ))
    conn.executemany(
        "INSERT OR REPLACE INTO matchups (season, week, roster_id, matchup_id, points)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def save_league_settings(conn, fmt, current_week):
    settings = {
        "superflex": str(fmt["superflex"]),
        "num_qbs": str(fmt["num_qbs"]),
        "ppr": str(fmt["ppr"]),
        "te_premium": str(fmt["te_premium"]),
        "starting_slots": json.dumps(fmt["starting_slots"]),
        "roster_positions": json.dumps(fmt["roster_positions"]),
        "draft_rounds": str(fmt["draft_rounds"]),
        "season": str(fmt["season"]),
        "playoff_teams": str(fmt["playoff_teams"]),
        "playoff_week_start": str(fmt["playoff_week_start"]),
        "current_week": str(current_week),
    }
    conn.executemany(
        "INSERT OR REPLACE INTO league_settings (key, value) VALUES (?, ?)",
        settings.items(),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    db.init_db()

    print(f"Fetching league {LEAGUE_ID}...")
    league = sleeper.fetch_league(LEAGUE_ID)
    fmt = derive_league_format(league)

    print("\nDetected league format:")
    print(f"  Superflex:    {fmt['superflex']}")
    print(f"  QB count:     {fmt['num_qbs']}")
    print(f"  PPR:          {fmt['ppr']}")
    print(f"  TE premium:   {fmt['te_premium']}")
    print(f"  Draft rounds: {fmt['draft_rounds']}")
    print(f"  Season:       {fmt['season']}")
    print(f"  Starting slots ({len(fmt['starting_slots'])}): {', '.join(fmt['starting_slots'])}")

    # Fetch all remote data before opening the DB transaction.
    # 1-second pause between requests to avoid Cloudflare rate limiting.
    print("\nFetching players...")
    players_data = sleeper.fetch_players()

    time.sleep(2)
    print("Fetching users...")
    users_data = sleeper.fetch_users(LEAGUE_ID)

    time.sleep(2)
    print("Fetching rosters...")
    rosters_data = sleeper.fetch_rosters(LEAGUE_ID)

    time.sleep(2)
    print("Fetching traded picks...")
    picks_data = sleeper.fetch_traded_picks(LEAGUE_ID)

    time.sleep(2)
    print("Fetching transaction history...")
    transactions_by_week = {}
    try:
        consecutive_empty = 0
        for week in range(1, 23):  # covers regular season + playoffs
            time.sleep(2)
            txns = sleeper.fetch_transactions(LEAGUE_ID, week)
            if txns:
                transactions_by_week[week] = txns
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
    except Exception as exc:
        print(f"  Warning: transaction fetch stopped at week {week}: {exc}")
        print("  Proceeding with partial or no transaction data.")

    time.sleep(2)
    print("Fetching NFL state...")
    nfl_state = sleeper.fetch_nfl_state()
    current_week = derive_current_week(nfl_state, fmt["playoff_week_start"])
    print(f"  season_type={nfl_state.get('season_type')} current_week={current_week}")

    print(f"Fetching matchups (weeks 1-{fmt['playoff_week_start'] - 1})...")
    matchups_by_week = {}
    for week in range(1, fmt["playoff_week_start"]):
        time.sleep(2)
        matchups_by_week[week] = sleeper.fetch_matchups(LEAGUE_ID, week)

    # Decide whether to fetch FC values (skip if already fetched today)
    conn = db.get_connection()
    today = datetime.now(timezone.utc).date().isoformat()
    last_fetch = conn.execute("SELECT MAX(fetched_at) FROM fc_values").fetchone()[0]
    fetch_fc = not (last_fetch and last_fetch[:10] == today)

    fc_data = None
    if fetch_fc:
        time.sleep(2)
        print("Fetching FantasyCalc values...")
        print(f"  (numQbs={fmt['num_qbs']}, ppr={fmt['ppr']})")
        fc_data = fantasycalc.fetch_values(fmt["num_qbs"], fmt["ppr"])
    else:
        print(f"FantasyCalc values already fetched today ({today}), skipping.")

    # Write everything atomically
    with conn:
        save_league_settings(conn, fmt, current_week)

        n = ingest_players(conn, players_data)
        print(f"\nUpserted {n} players")

        ingest_users(conn, users_data)
        print(f"Upserted {len(users_data)} users")

        ingest_rosters(conn, rosters_data)
        print(f"Upserted {len(rosters_data)} rosters")

        ingest_traded_picks(conn, picks_data)
        print(f"Ingested {len(picks_data)} traded picks")

        n = ingest_transactions(conn, fmt["season"], transactions_by_week)
        total_weeks = len(transactions_by_week)
        print(f"Ingested {n} transactions across {total_weeks} weeks")

        n = ingest_matchups(conn, fmt["season"], matchups_by_week)
        print(f"Ingested {n} matchup rows across {len(matchups_by_week)} weeks")

        if fc_data is not None:
            fetched_at = datetime.now(timezone.utc).isoformat()
            n = ingest_fc_values(conn, fc_data, fetched_at)
            print(f"Inserted {n} FantasyCalc values")

    conn.close()
    print("\nIngestion complete.")


if __name__ == "__main__":
    main()
