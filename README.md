# Dynasty GM

Dynasty fantasy football analysis tool for a Sleeper league.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

Fetch data from Sleeper and FantasyCalc into SQLite:

```bash
python ingest.py
```

Generate the roster strength report (includes playoff odds):

```bash
python report.py
```

Run the web app locally (trade builder, report, free agents, playoff odds):

```bash
python app.py
# then open http://127.0.0.1:5000 -- no password prompt locally (APP_PASSWORD unset)
```

## Deployment

The web app is designed to run on Render's free tier, reachable from any
device, with data updates driven from inside the app itself (no dependency
on a local machine being on). See `DEPLOY.md` for the one-time setup
checklist. Summary of what makes this work:

- **Auth**: a shared password gate (`APP_PASSWORD` env var) since the app
  is on a public URL.
- **Auto-refresh**: a background ingest kicks off automatically when the
  app wakes up (and on any page load if the last refresh is stale), plus
  a manual "Refresh Now" button. A full refresh takes ~1-2 minutes.
- **Persistence**: Render's free tier wipes local disk on every idle
  restart. `db_backup.py` works around this by treating a second, private
  GitHub repo as blob storage for `dynasty.db` -- restored on boot, saved
  after every successful ingest -- so value-trend history survives restarts.

## Notes

- `ingest.py` is idempotent — safe to re-run. FantasyCalc values are skipped if already fetched today.
- The Sleeper player dump (~5MB) is cached to `players_cache.json` and refreshed at most once per day.
- `dynasty.db` and `players_cache.json` are local data files; do not commit them.
- `playoff_sim.py` is a Monte Carlo simulation of the remaining regular
  season (10,000 runs) -- see its module docstring for the strength model.
