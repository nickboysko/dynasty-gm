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
```

## Architecture

```
dynasty-gm/
├── ingest.py           # Fetches Sleeper + FantasyCalc data, writes to SQLite
├── report.py           # 7-section roster strength report
├── trade_finder.py     # Trade partners, rebuild targets, sell-side market
├── target_finder.py    # "I want Player X — what should I offer?"
├── analyze_managers.py # Pick positions, trade value history, buy/sell signals
├── db.py               # Schema + migrations, DB connection helper
├── sleeper.py          # Sleeper API client (curl_cffi, Cloudflare bypass)
├── fantasycalc.py      # FantasyCalc API client
├── utils.py            # Shared helpers used by all scripts
├── ingest_daily.bat    # Batch file for Windows Task Scheduler (runs 7am daily)
├── dynasty.db          # SQLite database (gitignored)
├── players_cache.json  # /players/nfl cache (gitignored, refresh max once/day)
├── logs/               # Ingest log output from Task Scheduler
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

Package generation tries (1v1, 2v1, 1v2, 2v2) combos, filters by secondary asset floor and trivial pick-for-pick swaps, sorts by positional fit then value closeness.

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

## Future Work (Prioritized)

### Tier 1 — High impact, build next

1. **Free agent / waiver wire targets** — query FC values for players not on any roster; surface high-value free agents before opponents notice
2. **Trade offer evaluator** — input both sides of any inbound offer and get: fair/unfair, dynasty prime gap, who wins long-term
3. **Playoff schedule analyzer** — identify which players have favorable matchups during your league's specific playoff weeks (usually weeks 15-17)

### Tier 2 — Strategic edge

4. **LLM narrative layer** — call Claude API with roster + surplus data; output a plain-English paragraph: "your biggest lever is X, target Y, avoid trading Z"
5. **Draft class scouting overlay** — annotate your pick capital with 2026 NFL draft prospect rankings; e.g., "dannyleep7 Rd1 likely top-3 pick"
6. **Multi-team trade finder** — find 3-way deals where A has what you need, B has what A needs, etc.

### Tier 3 — Quality of life

7. **Web UI** — local Flask app so you can use this in a browser instead of terminal
8. **FAAB tracker** — if league uses auction waivers, track budget and recommend bids
