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

Generate the roster strength report:

```bash
python report.py
```

## Notes

- `ingest.py` is idempotent — safe to re-run. FantasyCalc values are skipped if already fetched today.
- The Sleeper player dump (~5MB) is cached to `players_cache.json` and refreshed at most once per day.
- `dynasty.db` and `players_cache.json` are local data files; do not commit them.
