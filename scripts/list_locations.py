"""Print all stock items grouped by their current Grocy location."""
import json, urllib.request, re, sys
from pathlib import Path

env = (Path(__file__).parents[1] / ".env").read_text()
base = re.search(r"FOODBRAIN_GROCY_BASE_URL=(\S+)", env).group(1)
key  = re.search(r"FOODBRAIN_GROCY_API_KEY=(\S+)", env).group(1)
h = {"GROCY-API-KEY": key}

def get(path):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(base + path, headers=h)).read())

locs = {str(l["id"]): l["name"] for l in get("/api/objects/locations")}
rows = get("/api/stock")

by_loc = {}
for r in rows:
    p = r["product"]
    loc = locs.get(str(p.get("location_id") or ""), "(none set)")
    by_loc.setdefault(loc, []).append((p["id"], p["name"], r.get("stock_amount", r.get("amount"))))

for loc in sorted(by_loc):
    print(f"\n=== {loc} ===")
    for pid, name, amt in sorted(by_loc[loc], key=lambda x: x[1]):
        print(f"  {pid:<5} {name:<40} {amt}")
