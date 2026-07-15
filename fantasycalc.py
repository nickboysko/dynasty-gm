import urllib.parse

from curl_cffi import requests

BASE_URL = "https://api.fantasycalc.com/values/current"


def fetch_values(num_qbs, ppr):
    """Fetches current dynasty player values from FantasyCalc."""
    params = urllib.parse.urlencode({
        "isDynasty": "true",
        "numQbs": num_qbs,
        "ppr": ppr,
    })
    resp = requests.get(f"{BASE_URL}?{params}", impersonate="chrome", timeout=30)
    resp.raise_for_status()
    return resp.json()
