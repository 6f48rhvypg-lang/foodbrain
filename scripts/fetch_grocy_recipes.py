"""Fetch live Grocy recipe objects and save them as a local bundle for diagnostics.

Reads FOODBRAIN_GROCY_BASE_URL and FOODBRAIN_GROCY_API_KEY from the environment
or a local .env file, then writes a single JSON object containing the recipes,
recipe positions (ingredients), products, and quantity units to a file
(default: .foodbrain-local/recipes.json). The bundle can then be matched or
diagnosed without committing secrets or household data:

    foodbrain --grocy-recipes-json .foodbrain-local/recipes.json --sample
    foodbrain --diagnose-grocy-recipes-json .foodbrain-local/recipes.json

Usage:
    PYTHONPATH=src python3 scripts/fetch_grocy_recipes.py
    PYTHONPATH=src python3 scripts/fetch_grocy_recipes.py .foodbrain-local/recipes.json
"""

from pathlib import Path
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from foodbrain_assistant.config import load_settings


_OBJECTS = {
    "recipes": "api/objects/recipes",
    "recipes_pos": "api/objects/recipes_pos",
    "products": "api/objects/products",
    "quantity_units": "api/objects/quantity_units",
}


def main(argv: list[str]) -> int:
    out_path = Path(argv[0]) if argv else Path(".foodbrain-local/recipes.json")

    settings = load_settings()
    if not settings.grocy_enabled:
        print(
            "Grocy is not configured. Set FOODBRAIN_GROCY_BASE_URL and "
            "FOODBRAIN_GROCY_API_KEY in .env or the environment.",
            file=sys.stderr,
        )
        return 2

    base = settings.grocy_base_url.rstrip("/") + "/"
    bundle = {}
    for key, path in _OBJECTS.items():
        payload = _get_json(urljoin(base, path), settings.grocy_api_key)
        if payload is None:
            return 1
        bundle[key] = payload

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    recipe_count = len(bundle["recipes"]) if isinstance(bundle["recipes"], list) else "?"
    print(f"Saved Grocy recipe bundle to {out_path} ({recipe_count} recipes).")
    return 0


def _get_json(url: str, api_key: str):
    request = Request(url, headers={"GROCY-API-KEY": api_key})
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        print(f"Grocy request to {url} failed with HTTP {exc.code}", file=sys.stderr)
    except URLError as exc:
        print(f"Grocy request to {url} failed: {exc.reason}", file=sys.stderr)
    except json.JSONDecodeError as exc:
        print(f"Grocy response from {url} was not valid JSON: {exc}", file=sys.stderr)
    return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
