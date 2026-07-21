# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dynasty-GM is a personal dynasty fantasy football GM assistant for a Sleeper league (ID: 1312107851514155008). The goal is to WIN the league — not a portfolio project. It ingests live data daily, reports roster strength, finds trades, and annotates every recommendation with dynasty-specific context (positional career curves, value trends, team tiers).

League format: **Superflex, 1.0 PPR, 0.5 TE premium, 4 draft rounds.**

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Fetch latest data from Sleeper + FantasyCalc (idempotent, safe to re-run)
python ingest.py

# Full roster strength report (7 sections)
python report.py

# Trade finder — shows positional partners, rebuild targets, sell-side market
python trade_finder.py

# Target a specific player — what should you offer to get them?
python target_finder.py "breece hall"
python target_finder.py bijan
python target_finder.py gibbs

# Manager trade profile analysis (pick positions, buy/sell signals)
python analyze_managers.py

# Interactive web trade builder — seed players/picks from either roster, get
# generated packages, edit them live (add/remove assets, see value update)
python app.py
# then open http://127.0.0.1:5000 (local only)
```

## Architecture

```
dynasty-gm/
├── ingest.py           # Fetches Sleeper + FantasyCalc data, writes to SQLite
├── report.py           # 7-section roster strength report
├── trade_finder.py     # Trade partners, rebuild targets, sell-side market, generate_packages(_seeded) engine
├── target_finder.py    # "I want Player X — what should I offer?"
├── analyze_managers.py # Pick positions, trade value history, buy/sell signals
├── app.py              # Flask web trade builder — seed/generate/edit packages live (127.0.0.1:5000)
├── templates/          # index.html for app.py
├── static/             # app.js + style.css for app.py
├── db.py               # Schema + migrations, DB connection helper
├── sleeper.py          # Sleeper API client (curl_cffi, Cloudflare bypass)
├── fantasycalc.py      # FantasyCalc API client
├── utils.py            # Shared helpers used by all scripts
├── ingest_daily.bat    # Batch file for Windows Task Scheduler (runs 7am daily)
├── dynasty.db          # SQLite database (gitignored)
├── players_cache.json  # /players/nfl cache (gitignored, refresh max once/day)
├── logs/               # Ingest log output from Task Scheduler (gitignored)
└── requirements.txt
```

## HTTP Client — Critical

**Always use `curl_cffi` with `impersonate="chrome"`.** Sleeper and FantasyCalc are behind Cloudflare Bot Management. `urllib` and `requests` are blocked. Never change this.

```python
from curl_cffi import requests
resp = requests.get(url, impersonate="chrome", timeout=30)
```

Retry delays: `[3, 6, 12]` seconds. Sleep 2s between sequential API calls. Catch `curl_cffi.requests.exceptions.RequestsError` (parent of SSLError, ConnectionError, etc.).

## Data Sources

**Sleeper API** (free, no auth): `https://api.sleeper.app/v1`
- `/league/1312107851514155008` — settings, roster_positions, scoring_settings
- `/league/1312107851514155008/rosters` — includes `settings.wins/losses/fpts`
- `/league/1312107851514155008/users`
- `/league/1312107851514155008/traded_picks`
- `/league/1312107851514155008/transactions/{week}` — trade/waiver history by week
- `/players/nfl` — ~5MB player dump. **Cache to `players_cache.json`, max once/day. Never call repeatedly.**

**FantasyCalc API** (free, no auth):
`https://api.fantasycalc.com/values/current?isDynasty=true&numQbs={1|2}&ppr={value}`
- Returns dynasty player values with Sleeper player IDs, including rookie picks.
- Build URL from derived league settings (never hardcode).

## League Format Detection

**Never hardcode league format.** Always derive from `/league/{id}` at runtime:
- `superflex` → `"SUPER_FLEX" in roster_positions` → `numQbs=2` for FantasyCalc
- `ppr` → `scoring_settings["rec"]`
- `te_premium` → `scoring_settings.get("bonus_rec_te", 0)`
- `starting_slots` → `roster_positions` excluding `"BN"`, `"IR"`, `"TAXI"`

## Database Schema

All schema lives in `db.py`. `init_db()` is idempotent — call it at the start of every script so migrations run automatically. Report/trade scripts call `db.init_db()` before `db.get_connection()`.

Key conventions:
- `fc_values` is **append-only** (INSERT only, never UPDATE/DELETE) — enables value trend tracking. Query via `MAX(fetched_at)` for current values.
- `rosters` stores `wins/losses/ties/fpts` from Sleeper — added via `ALTER TABLE` migration in `init_db()`.
- `traded_picks`: `original_roster_id` = Sleeper `roster_id` (whose slot), `current_roster_id` = Sleeper `owner_id` (who holds it now).
- `transactions`: trade/waiver/FA history by week, keyed by `transaction_id`. INSERT OR REPLACE.

## utils.py — Shared Logic

Everything shared across scripts lives here. Key exports:

| Symbol | Purpose |
|---|---|
| `POSITIONS` | `["QB", "RB", "WR", "TE"]` |
| `PRIME_CLIFF_AGE` | `{"RB": 27, "WR": 30, "TE": 29, "QB": 33}` — research-backed cliff ages |
| `prime_years_remaining(player)` | Seasons until positional cliff; `None` if age unknown |
| `load_settings(conn)` | Reads `league_settings` table into a dict |
| `get_latest_fc_values(conn)` | Returns `{player_id: {value, name, position}}` at latest fetch |
| `get_value_trends(conn, days=7)` | Compares latest vs oldest fetch in window; returns `{}` if <2 dates |
| `get_rosters(conn, fc_values)` | Rosters with player lists enriched with FC values + wins/losses |
| `assign_starters(players, slots)` | Greedy: QB→RB→WR→TE→K→DEF→FLEX→SUPER_FLEX |
| `build_pick_value_table(fc_values)` | `(year_str, round_int) -> avg_value` from FC pick entries |
| `classify_teams(rosters, slots)` | Tier score: 60/40 value (offseason) or 40/20/40 with win rate (in-season) |
| `compute_pick_assets(conn, rosters, rounds, pick_values)` | `{roster_id: [pick_asset_dicts]}` for future seasons |

## Trade Engine Logic (trade_finder.py / target_finder.py)

Key constants:
```python
MIN_ASSET_VALUE = 500          # ignore low-value assets in packages
CONSOLIDATION_PREMIUM = 0.10   # fewer concentrated assets get 10% effective boost
SECONDARY_ASSET_FLOOR = 0.20   # every asset in a combo must be >= 20% of the top asset
PICK_PREMIUM_REBUILD = 0.20    # picks inflated 20% when offered to rebuilders
PLAYER_DISCOUNT_REBUILD = 0.10 # rebuilder players discounted 10% (willing sellers)
TOLERANCE = 0.22               # +/-22% value window for "fair" trade
```

Package generation tries (1v1, 2v1, 1v2, 2v2) combos, filters by secondary asset floor and trivial pick-for-pick swaps, then sorts by: **(1) strategy alignment for your team's tier, (2) positional fit, (3) value closeness.**

**Strategy alignment** (`generate_packages`/`generate_packages_seeded`'s `my_tier` param, via `_strategy_alignment_penalty`): Contending teams rank send>recv ("give up depth for one great player") packages first; Rebuilding teams rank recv>send ("sell one good player for multiple/younger assets") first; Middle has no directional preference. This is the dominant sort key -- it applies before positional fit or value closeness, in `trade_finder.py`, `target_finder.py`, and `app.py` alike.

**Dynasty warnings** fire on every package when prime-seasons gap >= 3 between send and receive sides. RBs cliff at 27, WRs at 30 — the same market value can hide a large future gap. These are informational; market values are still used for matching.

Team tiers (Contending / Middle / Rebuilding):
- Score = 60% starter rank + 40% total rank (offseason, no record)
- Score = 40% starter rank + 20% total rank + 40% win rate (in-season)
- Contending >= 0.60, Rebuilding <= 0.35

## Daily Automation

`ingest_daily.bat` runs `python ingest.py` and appends to `logs\ingest.log`. Registered as "DynastyGM Daily Ingest" in Windows Task Scheduler at 7:00 AM daily. Value trend features activate once 2+ distinct fetch dates exist in `fc_values`.

## Positional Career Curve Research Summary

From Apex Fantasy Leagues, Fantasy Footballers, 4for4, PFF (validated across multiple datasets):
- **RBs**: modern-era peak age 24.8, cliff at 27-28, only 7.8% of elite seasons at 29+
- **WRs**: modern-era peak age 26.0, cliff at 30-32, still 74% of baseline at age 33
- **Implication**: a 23-year-old RB has ~4 prime seasons; a 23-year-old WR has ~7. Same market value ≠ same dynasty value.

## What's Built

| Script | What it does |
|---|---|
| `ingest.py` | Daily Sleeper + FC fetch; transaction history; idempotent |
| `report.py` | 7 sections: total value, positional value, starters vs bench, age, pick capital, value movers (needs trend data), strategy assessment |
| `trade_finder.py` | Positional partners, rebuild targets, sell-side market; dynasty + trend annotations |
| `target_finder.py` | Input any player name, get fair packages from your roster to acquire them |
| `analyze_managers.py` | Pick capital positions, trade value history, buy/sell signals |
| `app.py` | Web UI, three tabs: **Trade Builder** (pick a partner, seed players/picks from either roster, generate packages, edit any package live with instant fairness/dynasty/trend/surplus-impact feedback; "Find Trades (All Teams)" searches your top 5 ranked partners at once using only your-side seeds; "Copy for AI" exports a package as plain text -- both teams' tier/record plus your full roster -- for pasting into any AI chat), **Report** (the same 7 sections as `report.py`, reusing its `compute_*` functions), and **Free Agents** (every unrostered player with FC value > 0, search + position filter, "vs Your Roster" upgrade comparison against your weakest player at that position, "Suggested Pickups" callout) |

## Untouchables

`utils.UNTOUCHABLES` (a set of lowercased `full_name`s) marks players who should never appear in an *automatically generated* sendable pool — `filter_untouchables()` excludes them from `trade_finder.py`, `target_finder.py`, and `app.py`'s auto-fill pools. This does not block manually including one of them in a trade — in `app.py`, checking an untouchable as a seed or adding it via a package card's "+ Add asset" still evaluates normally; the guard is only on the algorithm's own suggestions.

## Future Work (Prioritized)

### Tier 1 — High impact, build next

1. **Injury/status awareness** — Sleeper's `/players/nfl` dump includes injury status, but the `players` table doesn't store it and nothing in `report.py`/`app.py` surfaces it. Small effort (one more ingested field + a badge in Free Agents/trade views), but real downside without it: recommending a trade target or waiver pickup with no idea they're questionable/out/IR becomes an active risk once the season starts. Do this first — it's cheap and closes a real gap.
2. **Weekly start/sit + matchup awareness** — everything built so far (trade builder, report, free agents) is roster-*construction*. Once games start, the recurring decision that actually wins weeks is "who do I start," which depends on matchups/byes — the tool currently has zero visibility into this. Bigger lift than #1 (needs a schedule/matchup data source), but it's a genuinely new capability, not a refinement of what exists. **Subsumes** the older "playoff schedule analyzer" idea (favorable matchups in weeks 15-17) — build this once, not twice.
3. **Recent league activity feed** — `ingest.py` already pulls every trade/waiver transaction into the `transactions` table weekly; nothing surfaces it. A simple "what did the league do this week" view is cheap (data's already there) and gives real intel on rivals tipping their hand (e.g. a rebuilder loading up on rookie RBs confirms their direction before you negotiate).

### Tier 2 — Strategic edge

4. **Draft class scouting overlay** — annotate pick capital with NFL draft prospect rankings. Low urgency right now: the tradeable picks are all future-season (2027+), and there's no real scouting data on a class whose college season hasn't happened yet. Revisit next spring closer to the actual draft.
5. **Multi-team trade finder** — find true 3-way deals where A has what B needs, etc. (`app.py`'s single-trade builder, and even "Find Trades All Teams," only ever construct 2-team deals). Lower priority — 3-way trades are rare in practice even when supported.
6. **AI trade-advice endpoint** — deferred: user is unsure how long they'll keep paying for Claude Pro / API usage, so `app.py`'s "Copy for AI" button (paste a generated package summary into any AI chat manually, no API key needed) covers this need for now. Revisit only if the user wants it automated later — don't suggest this unprompted.

### Tier 3 — Quality of life

7. **Visual polish on `app.py`** — current styling is functional, not pretty; revisit once the feature set above settles
8. **FAAB tracker** — if league uses auction waivers, track budget and recommend bids (unconfirmed whether this league does — check league settings before starting)
