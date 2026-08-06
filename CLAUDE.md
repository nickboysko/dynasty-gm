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

# Full roster strength report (8 sections, incl. playoff odds)
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
# then open http://127.0.0.1:5000 (no password prompt locally -- APP_PASSWORD unset)
```

`app.py` also has a **Playoff Odds** tab (Monte Carlo sim of the remaining season,
`playoff_sim.py`) and auto-refreshes its data in the background (a manual
"Refresh Now" button is also available) -- see "Deployment" below for how this
runs unattended once hosted.

## Architecture

```
dynasty-gm/
├── ingest.py           # Fetches Sleeper + FantasyCalc data, writes to SQLite
├── report.py           # 8-section roster strength report
├── trade_finder.py     # Trade partners, rebuild targets, sell-side market, generate_packages(_seeded) engine
├── target_finder.py    # "I want Player X — what should I offer?"
├── analyze_managers.py # Pick positions, trade value history, buy/sell signals
├── playoff_sim.py      # Monte Carlo playoff-odds simulator (real schedule + team strength)
├── app.py              # Flask web app — trade builder, report, free agents, playoff odds
├── db_backup.py         # Backs up/restores dynasty.db via a private GitHub repo (Render persistence)
├── templates/          # index.html + login.html for app.py
├── static/             # app.js + style.css for app.py
├── db.py               # Schema + migrations, DB connection helper
├── sleeper.py          # Sleeper API client (curl_cffi, Cloudflare bypass)
├── fantasycalc.py      # FantasyCalc API client
├── utils.py            # Shared helpers used by all scripts
├── ingest_daily.bat    # Batch file for Windows Task Scheduler (runs 7am daily, local dev only)
├── render.yaml         # Render Blueprint (deployment config)
├── gunicorn.conf.py     # post_fork hook -- runs startup/refresh inside the worker, not the master
├── DEPLOY.md           # One-time manual setup checklist for Render deployment
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
- `matchups`: one row per roster per week (`season, week, roster_id` PK), `matchup_id` pairs the two rosters that played each other that week, `points` is that roster's score. INSERT OR REPLACE every ingest. Weeks `1..playoff_week_start-1` are fetched every run regardless of whether they've been played -- Sleeper pre-generates the full regular-season schedule before it's played. `league_settings` also carries `playoff_teams`, `playoff_week_start`, and `current_week` (derived from `/state/nfl`, since `points == 0` alone can't distinguish a real shutout from an unplayed week) -- see `playoff_sim.py`.

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

`ingest_daily.bat` runs `python ingest.py` and appends to `logs\ingest.log`. Registered as "DynastyGM Daily Ingest" in Windows Task Scheduler at 7:00 AM daily. Value trend features activate once 2+ distinct fetch dates exist in `fc_values`. This is a **local-only** convenience for running scripts against your own machine's `dynasty.db` -- the deployed web app (below) has its own independent auto-refresh and doesn't depend on this task or your PC being on.

## Deployment (2026-08-06)

`app.py` is deployed on Render's free tier (`render.yaml`, gunicorn, `--workers 1` -- required, not tunable, since `STATE`/the update-lock/the background thread are in-process singletons). See `DEPLOY.md` for the one-time manual setup checklist (GitHub repos, Render env vars, PAT scoping). Key mechanics:

- **Auth**: shared-password session gate (`APP_PASSWORD` + `SECRET_KEY` env vars), no-op if unset (local dev).
- **Worker startup**: `app.py`'s `start_worker()` (restore -> init_db -> load_state -> first background refresh) is called from `gunicorn.conf.py`'s `post_fork` hook, **not** at bare module-import time. Found the hard way (2026-08-06): gunicorn 26's master process imports `app.py` to validate the `app:app` callable before forking any workers -- if startup ran unconditionally at import time, its background thread would spawn and run to completion *inside the master*, which never serves HTTP (fork() doesn't carry running threads into the child worker). The worker that actually handles every request would inherit a frozen pre-fork snapshot of `STATE`/`UPDATE_STATUS` and never see any of that work -- classic split-brain, diagnosed by logging `os.getpid()`/`id(UPDATE_STATUS)` from both the background thread and the request handler and finding they didn't match. `start_worker()` is also called directly from `python app.py`'s `__main__` block for local dev, where there's no fork to worry about.
- **Auto-refresh**: a background thread runs `ingest.main()` -> `load_state()` -> `db_backup.backup()`, kicked off via `start_worker()` at worker boot, on any page load once the last refresh is >15 min old, or via the "Refresh Now" button (`POST /api/update/trigger`, polled via `GET /api/update/status`, which also reports `started_at` so the UI can show elapsed time). Never blocks a request -- a full ingest takes ~60-120s.
- **Persistence**: Render's free tier wipes local disk on every idle restart (~15 min), which would silently break `fc_values` trend tracking (needs 2+ distinct fetch dates) if left unaddressed. `db_backup.py` works around this with a dedicated **second, private** GitHub repo (never connected to Render's deploy trigger) used purely as blob storage for `dynasty.db` -- `restore()` before `db.init_db()` at startup, `backup()` after each successful ingest, orphan-commit + force-push so it stays at exactly one commit. No-op unless its three env vars are set.
- **Cost**: free tier accepted deliberately -- cold start (~30-60s) plus a fresh background ingest on every ~15-min-idle wake is a known, accepted tradeoff, not a bug.

## Positional Career Curve Research Summary

From Apex Fantasy Leagues, Fantasy Footballers, 4for4, PFF (validated across multiple datasets):
- **RBs**: modern-era peak age 24.8, cliff at 27-28, only 7.8% of elite seasons at 29+
- **WRs**: modern-era peak age 26.0, cliff at 30-32, still 74% of baseline at age 33
- **Implication**: a 23-year-old RB has ~4 prime seasons; a 23-year-old WR has ~7. Same market value ≠ same dynasty value.

## What's Built

| Script | What it does |
|---|---|
| `ingest.py` | Daily Sleeper + FC fetch; transaction history; idempotent |
| `report.py` | 8 sections: total value, positional value, starters vs bench, age, pick capital, value movers (needs trend data), strategy assessment, playoff odds (`playoff_sim.py`) |
| `trade_finder.py` | Positional partners, rebuild targets, sell-side market; dynasty + trend annotations |
| `target_finder.py` | Input any player name, get fair packages from your roster to acquire them |
| `analyze_managers.py` | Pick capital positions, trade value history, buy/sell signals |
| `playoff_sim.py` | Monte Carlo simulation (10,000 runs) of the remaining regular season using the real Sleeper schedule + a team-strength model (value-based prior, blending toward empirical weekly scoring as real weeks accumulate). Reports each team's % chance of making the playoffs. v1 is a standalone view; "how much does trading for X move my odds" is a deferred fast-follow, not yet wired into the trade builder. |
| `app.py` | Web UI, four tabs: **Trade Builder** (pick a partner, seed players/picks from either roster, generate packages, edit any package live with instant fairness/dynasty/trend/surplus-impact feedback; "Find Trades (All Teams)" searches your top 5 ranked partners at once using only your-side seeds; "Copy for AI" exports a package as plain text -- both teams' tier/record plus your full roster -- for pasting into any AI chat), **Report** (the same 8 sections as `report.py`, reusing its `compute_*` functions), **Free Agents** (every unrostered player with FC value > 0, search + position filter, "vs Your Roster" upgrade comparison against your weakest player at that position, "Suggested Pickups" callout), and **Playoff Odds** (`playoff_sim.py`'s output). Password-gated when `APP_PASSWORD` is set (no-op locally); data auto-refreshes via a background ingest kicked off on boot and on any stale page load, plus a manual "Refresh Now" button -- see "Deployment" below. |
| `db_backup.py` | Backs up/restores `dynasty.db` via a second, private GitHub repo (git-as-blob-storage, orphan-commit + force-push so it never accumulates history) -- works around Render's free tier wiping local disk on every idle restart, so value-trend and transaction history survive. No-op unless `GITHUB_TOKEN`/`GITHUB_DATA_REPO_OWNER`/`GITHUB_DATA_REPO_NAME` are set. |

**Injury/status awareness** (2026-07-23): `players` table stores `status`/`injury_status`/`injury_body_part` from Sleeper's player dump; `utils.injury_flag()` derives a short badge (`Q`, `D`, `O`, `IR`, `PUP`, etc.) shown everywhere a player appears -- CLI trade output (`trade_finder.py`/`target_finder.py`), the Free Agents table's Status column, roster/trade-builder chips in `app.py` (color-coded by severity), and the Copy-for-AI text export.

## Untouchables

`utils.UNTOUCHABLES` (a set of lowercased `full_name`s) marks players who should never appear in an *automatically generated* sendable pool — `filter_untouchables()` excludes them from `trade_finder.py`, `target_finder.py`, and `app.py`'s auto-fill pools. This does not block manually including one of them in a trade — in `app.py`, checking an untouchable as a seed or adding it via a package card's "+ Add asset" still evaluates normally; the guard is only on the algorithm's own suggestions.

## Future Work (Prioritized)

### Tier 1 — High impact, build next

Researched 2026-07-23 (see "win-focused brainstorm" below for full context):

1. ~~**Playoff odds simulator**~~ — **Built 2026-08-06** (`playoff_sim.py`). Monte Carlo simulation of the remaining season using team strength (value-based prior, blending toward empirical weekly scoring as real weeks accumulate) + the real remaining schedule from Sleeper. v1 is a standalone view (`app.py`'s Playoff Odds tab, `report.py`'s 8th section) -- **surfacing trade-value-in-playoff-odds-terms directly in `trade_finder.py`/`app.py`'s trade builder is still an explicit fast-follow, not yet built.**
2. **Touchdown/efficiency regression detector** — compare actual TDs to expected TDs (from red zone opportunity / target share / carry share) to flag sell-high (overperforming volume) and buy-low (underperforming volume) candidates. This is a real, well-documented signal (unlike coverage-scheme matchup data, which research showed doesn't hold up — see below) and plugs directly into the existing sell-side-market/rebuild-target sections of `trade_finder.py`. Needs `nfl_data_py`/`nflverse` play-by-play data (the same dependency identified during start/sit research, already vetted -- see below) joined onto the existing `players` table via `nfl_data_py`'s `import_ids()`, which includes a `sleeper_id` column for a clean join — no fuzzy name-matching needed.
3. **Recent league activity feed** — `ingest.py` already pulls every trade/waiver transaction into the `transactions` table weekly; nothing surfaces it. A simple "what did the league do this week" view is cheap (data's already there) and gives real intel on rivals tipping their hand (e.g. a rebuilder loading up on rookie RBs confirms their direction before you negotiate).

### Researched and deprioritized — don't rebuild without new information

- **Weekly start/sit + matchup awareness** (researched 2026-07-23, not building). The obvious version (replicate DVP/projections) isn't worth building -- Yahoo/ESPN's own default projections already do usage-share regression + efficiency + context, so matching that is matching what already exists. The exciting version (man/zone coverage-scheme matchup data, e.g. via `nflverse`/FTN charting) turned out weaker than expected: the most rigorous public study of it (Fantasy Points/Scott Barrett) found coverage-filtered stats predict *worse* than unfiltered season stats for median fantasy output -- schematic matchup edges only move the needle on ceiling/boom outcomes, not the expected value of a start/sit call. More fundamentally, weekly fantasy scoring has enormous irreducible variance (e.g. a WR averaging 16.6 PPG can carry a ~13-point standard deviation) that's a property of the sport, not a solvable data problem -- consistent with "reliable" vs "unreliable" ranking experts scoring only 0.589 vs 0.550 in FantasyPros' own accuracy tracking. User's call after seeing this: for a close two-player decision the effort-to-value ratio isn't there ("I'll pick one, it's mostly luck either way"). Subsumes the older "playoff schedule analyzer" idea (favorable matchups weeks 15-17) -- same fate.

### Tier 2 — Strategic edge

4. **FAAB bid advisor** — confirmed 2026-07-23 via live Sleeper league settings that this league uses FAAB (`waiver_type: 2`, `waiver_budget: 100`), so this is definitely applicable (was previously listed as unconfirmed). Real, research-backed tactics to encode: bid non-round numbers ($16/$21 beats round-number bids like $15/$20 at the same effective spend), be aggressive with budget early-season (a Week 3 pickup has ~14 weeks of runway vs. a Week 12 one), and weight opportunity/snap-share over the prior week's box score. Data needed (waiver bid amounts, snap share) is already flowing through `transactions` and can reuse the free-agent infrastructure in `app.py`.
5. **Draft class scouting overlay** — annotate pick capital with NFL draft prospect rankings. If/when revisited, the concrete metric to build around is **breakout age / college dominator rating** (dominator = share of team yards+receptions+TDs; breakout = age at which a prospect first hit a 20%+ dominator rating) — real predictive research behind it (WRs breaking out at 18 hit at 38.5% in the NFL vs. 8.9% for age-21 breakouts). Still low urgency: tradeable picks are all future-season (2027+) and there's no scouting data yet on a class that hasn't played its college season. Revisit next spring.
6. **Manager behavioral tendency exploitation** — extend `analyze_managers.py` to flag concrete patterns (e.g. "this manager historically overpays right after a player's big game") using `transactions` timestamps vs. performance data. Real concept, but `analyze_managers.py` already notes this league has only ~3 trades ever — likely too little history to separate a real pattern from noise right now. Revisit once more trade history accumulates.
7. **Multi-team trade finder** — find true 3-way deals where A has what B needs, etc. (`app.py`'s single-trade builder, and even "Find Trades All Teams," only ever construct 2-team deals). Lower priority — 3-way trades are rare in practice even when supported.
8. **AI trade-advice endpoint** — deferred: user is unsure how long they'll keep paying for Claude Pro / API usage, so `app.py`'s "Copy for AI" button (paste a generated package summary into any AI chat manually, no API key needed) covers this need for now. Revisit only if the user wants it automated later — don't suggest this unprompted.

### Tier 3 — Quality of life

9. **Visual polish on `app.py`** — current styling is functional, not pretty; revisit once the feature set above settles
