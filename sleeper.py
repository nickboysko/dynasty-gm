import json
import os
import time

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException as CurlError

BASE_URL = "https://api.sleeper.app/v1"
PLAYERS_CACHE = os.path.join(os.path.dirname(__file__), "players_cache.json")
CACHE_MAX_AGE = 86400  # 24 hours
_RETRY_DELAYS = [3, 6, 12]  # seconds between attempts 1→2, 2→3, 3→fail


def _get(path):
    url = f"{BASE_URL}{path}"
    last_exc = None
    for i, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            print(f"  Retrying in {delay}s (attempt {i + 1}/4)...")
            time.sleep(delay)
        try:
            resp = requests.get(url, impersonate="chrome", timeout=30)
            resp.raise_for_status()
            return resp.json()
        except CurlError as exc:
            last_exc = exc
    raise last_exc


def fetch_league(league_id):
    return _get(f"/league/{league_id}")


def fetch_rosters(league_id):
    return _get(f"/league/{league_id}/rosters")


def fetch_users(league_id):
    return _get(f"/league/{league_id}/users")


def fetch_traded_picks(league_id):
    return _get(f"/league/{league_id}/traded_picks")


def fetch_transactions(league_id, week):
    """Returns list of transactions for the given week (1-indexed NFL week)."""
    return _get(f"/league/{league_id}/transactions/{week}")


def fetch_players():
    """Returns the full NFL player dict, reading from cache if it is less than 24 hours old."""
    if os.path.exists(PLAYERS_CACHE):
        if time.time() - os.path.getmtime(PLAYERS_CACHE) < CACHE_MAX_AGE:
            with open(PLAYERS_CACHE) as f:
                return json.load(f)

    print("Fetching player data from Sleeper (~5 MB, cached for 24 h)...")
    data = _get("/players/nfl")
    with open(PLAYERS_CACHE, "w") as f:
        json.dump(data, f)
    return data
