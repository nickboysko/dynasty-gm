# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dynasty-GM is a dynasty fantasy football AI GM assistant for a Sleeper league. It values players, analyzes roster strength, and will eventually suggest trades and advise on rebuild-vs-compete decisions.

**Current scope: Phase 1 only** — data ingestion + roster strength report. Do not build the trade finder, win-now scoring, LLM integration, or any UI.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run ingestion (idempotent — safe to re-run)
python ingest.py

# Run roster report
python report.py
```

## Architecture

```
dynasty-gm/
├── ingest.py        # Fetches Sleeper + FantasyCalc data, writes to SQLite
├── report.py        # Queries DB, prints roster strength report
├── db.py            # Schema creation, DB connection helper
├── sleeper.py       # Sleeper API client
├── fantasycalc.py   # FantasyCalc API client
├── dynasty.db       # SQLite database (gitignored)
├── players_cache.json  # /players/nfl cache (gitignored, refresh max once/day)
├── requirements.txt
└── README.md
```

## Data Sources

**Sleeper API** (free, no auth): `https://api.sleeper.app/v1`
- `/league/1312107851514155008` — settings, roster_positions, scoring_settings
- `/league/1312107851514155008/rosters`
- `/league/1312107851514155008/users`
- `/league/1312107851514155008/traded_picks`
- `/players/nfl` — ~5MB player dump. **Cache to `players_cache.json`, refresh max once per day. Never call this repeatedly.**

**FantasyCalc API** (free, no auth):
`https://api.fantasycalc.com/values/current?isDynasty=true&numQbs={1|2}&ppr={value}`
- Returns dynasty player values with Sleeper player IDs attached, including rookie picks
- Build URL from derived league settings (see below)

## League Format Detection

**Never hardcode league format.** Always derive from `/league/{id}` at runtime:
- `superflex` → `"SUPER_FLEX" in roster_positions` → `numQbs=2` for FantasyCalc, else `1`
- `ppr` → `scoring_settings["rec"]`
- `te_premium` → `scoring_settings.get("bonus_rec_te", 0)`
- `starting_slots` → `roster_positions` excluding `"BN"`, `"IR"`, `"TAXI"`

Print detected format on first run so it can be sanity-checked.

## Database Schema Conventions

- The `values` table is **append-only** with a `fetched_at` timestamp — this enables value trend tracking in later phases. Never update or delete rows; always insert new ones.
- Use stdlib `sqlite3` — no ORM.

## Phase 1 Report Output

The report script outputs (printed table or JSON):
1. Total roster value ranking for all teams
2. Per-team value by position vs. league median
3. Starter value vs. bench value (based on derived starting lineup slots)
4. Value-weighted average age per team
5. Draft pick capital per team (using FantasyCalc pick values)

## Tech Constraints

- Python 3.11+, `requests`, `sqlite3` (stdlib)
- No web framework, no ORM, no async
- Simple and readable over clever — this is a portfolio project

## Future Phases (do not build yet)

- Trade finder and realistic trade targets
- Win-now vs. rebuild scoring
- Manager trade-pattern profiling
- LLM analysis layer
- UI
