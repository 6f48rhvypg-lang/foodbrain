"""Fetch the live Grocy /api/stock response and save it locally for diagnostics.

Reads FOODBRAIN_GROCY_BASE_URL and FOODBRAIN_GROCY_API_KEY from the environment
or a local .env file, then writes the raw JSON payload to a file (default:
.foodbrain-local/stock.json) so it can be inspected and diagnosed without
committing secrets or household data.

Usage:
    PYTHONPATH=src python3 scripts/fetch_grocy_stock.py
    PYTHONPATH=src python3 scripts/fetch_grocy_stock.py .foodbrain-local/stock.json
"""

from pathlib import Path
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from foodbrain_assistant.config import load_settings


def main(argv: list[str]) -> int:
    out_path = Path(argv[0]) if argv else Path(".foodbrain-local/stock.json")

    settings = load_settings()
    if not settings.grocy_enabled:
        print(
            "Grocy is not configured. Set FOODBRAIN_GROCY_BASE_URL and "
            "FOODBRAIN_GROCY_API_KEY in .env or the environment.",
            file=sys.stderr,
        )
        return 2

    url = urljoin(settings.grocy_base_url.rstrip("/") + "/", "api/stock")
    request = Request(url, headers={"GROCY-API-KEY": settings.grocy_api_key})
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        print(f"Grocy request failed with HTTP {exc.code}", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Grocy request failed: {exc.reason}", file=sys.stderr)
        return 1

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Grocy response was not valid JSON: {exc}", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    row_count = len(payload) if isinstance(payload, list) else "unknown"
    print(f"Saved Grocy /api/stock response to {out_path} ({row_count} rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
