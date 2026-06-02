# FoodBrain Assistant

A self-hosted kitchen decision helper for reducing food waste, discovering flavor combinations, and turning pantry inventory into practical meal suggestions.

The project combines:

- Grocy for grocery stock, expiry dates, shopping lists, and recipes
- Home Assistant for notifications, dashboards, and household automation
- FlavorGraph for ingredient pairing intelligence
- Open food datasets for barcode, nutrition, and ingredient metadata

## Goal

Help answer the recurring question:

> What should I cook, buy, or use today based on what I already have and what expires soon?

## Core Ideas

- Track food inventory and expiry dates
- Rank ingredients by urgency
- Suggest meals that use soon-to-expire items
- Recommend compatible flavor pairings
- Generate missing shopping-list items
- Surface suggestions in Home Assistant

## Initial Stack

- Proxmox server
- Home Assistant
- Grocy
- Python sidecar service
- MQTT or REST bridge into Home Assistant
- FlavorGraph embeddings

## Project Status

Initial implementation started.

Current baseline:

- Python package under `src/foodbrain_assistant`
- Environment-based configuration
- Minimal Grocy `/api/stock` client
- Fixture-driven Grocy `/api/stock` response parser diagnostics
- Expiry-aware ingredient urgency scoring
- Recipe matching against stock, ranked by pantry coverage and expiry usefulness
- Optional Home Assistant webhook publishing
- CLI for sample and live runs
- Unit tests for Grocy parsing, normalization, scoring, recipe parsing, and matching
- Git repository initialized and pushed to `https://github.com/RSM-CEI/foodbrain`

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests
foodbrain --sample
```

To verify parsing against an exported Grocy `/api/stock` response without committing
secrets or household data, save the response as a local ignored JSON file and run
the automatic diagnostics:

```bash
mkdir -p .foodbrain-local
foodbrain --diagnose-grocy-stock-json .foodbrain-local/stock.json
```

The diagnostic command exits with a non-zero status when the payload shape does not
match the parser contract. After diagnostics pass, run the recommendation flow with:

```bash
foodbrain --grocy-stock-json .foodbrain-local/stock.json --json
```

You can also run the optional real-data parser contract test without committing the
payload:

```bash
FOODBRAIN_GROCY_STOCK_JSON=.foodbrain-local/stock.json python -m unittest tests.test_grocy_real_stock_contract
```

To match recipes against the chosen stock, pass a local recipes JSON file. The
file is a list of recipes (or `{"recipes": [...]}`), each with a `name` and an
`ingredients` list of plain lines (`"2 cups flour"`) or objects
(`{"name": "Eggs", "quantity": 3, "unit": "pieces"}`). A sample lives at
[examples/recipes.sample.json](examples/recipes.sample.json):

```bash
foodbrain --sample --recipes-json examples/recipes.sample.json
```

Recipes are ranked by how much of each recipe is already in stock (`coverage`)
and how much soon-to-expire stock they use (`expiry_usefulness`). The same
`--recipes-json` flag works with `--grocy-stock-json` and live Grocy runs.

You can also match recipes stored in Grocy itself. Export the recipe objects to
a local ignored bundle, diagnose it, then match it against any stock source:

```bash
mkdir -p .foodbrain-local
PYTHONPATH=src python3 scripts/fetch_grocy_recipes.py
foodbrain --diagnose-grocy-recipes-json .foodbrain-local/recipes.json
foodbrain --sample --grocy-recipes-json .foodbrain-local/recipes.json
```

For a fully live run, fetch both stock and recipes from Grocy directly:

```bash
foodbrain --grocy-recipes
```

For a live Grocy run, copy `.env.example` to `.env`, fill in the values, then run:

```bash
foodbrain
```

Shell-exported environment variables override values in `.env`, which is useful for one-off runs or service managers.

Required environment variables for live Grocy access:

- `FOODBRAIN_GROCY_BASE_URL`
- `FOODBRAIN_GROCY_API_KEY`

Optional environment variables:

- `FOODBRAIN_HOME_ASSISTANT_WEBHOOK_URL`
- `FOODBRAIN_EXPIRY_WINDOW_DAYS`
- `FOODBRAIN_TOP_INGREDIENT_LIMIT`
- `FOODBRAIN_TOP_RECIPE_LIMIT`

## Current Development Plan

1. Stock ingestion is confirmed against real Grocy data (Phase 3 done).
2. Recipe matching is implemented for local files and Grocy recipes (Phase 4).
3. Next: verify Grocy recipe matching against real household data via `foodbrain --diagnose-grocy-recipes-json` / `--grocy-recipes`.
4. Then add FlavorGraph embeddings (Phase 5), with Home Assistant MQTT and Mealie/Tandoor sources as optional follow-ups.

## Next Session Handoff

Use [handoff.md](handoff.md) as the primary restart note for the next session.

Current repo state:

- GitHub remote: `https://github.com/RSM-CEI/foodbrain`
- Branch: `main`
- Local verification command: `PYTHONPATH=src python3 -m unittest discover -s tests`
- Sample run command: `PYTHONPATH=src python3 -m foodbrain_assistant.cli --sample`
- Live run command after creating `.env`: `PYTHONPATH=src python3 -m foodbrain_assistant.cli`
- Grocy diagnostics command: `PYTHONPATH=src python3 -m foodbrain_assistant.cli --diagnose-grocy-stock-json .foodbrain-local/stock.json`

Start the next session by checking:

```bash
git status --short --branch
git pull --ff-only
PYTHONPATH=src python3 -m unittest discover -s tests
```

On another PC, clone or pull `https://github.com/RSM-CEI/foodbrain`, copy `.env.example` to `.env`, fill in the local Grocy URL and API key, then run the live command above. Keep `.env` and `.foodbrain-local/` private.

Next implementation steps:

1. Connect to a real Grocy instance by setting `FOODBRAIN_GROCY_BASE_URL` and `FOODBRAIN_GROCY_API_KEY`.
2. Export or save the live `/api/stock` response locally under `.foodbrain-local/`, then run `foodbrain --diagnose-grocy-stock-json .foodbrain-local/stock.json` or `FOODBRAIN_GROCY_STOCK_JSON=.foodbrain-local/stock.json python -m unittest tests.test_grocy_real_stock_contract`.
3. Run `foodbrain` against live stock after the saved response parses correctly.
4. Adjust `foodbrain_assistant.grocy_client` if the real `/api/stock` response shape differs from the current parser assumptions.
5. Decide the Home Assistant integration path:
	- Keep webhook publishing if one daily summary is enough.
	- Add MQTT publishing if dashboard sensors/entities are needed.
6. After stock parsing is confirmed, begin Phase 4 recipe matching with local recipe fixtures before calling live recipe APIs.

Important constraints for the next session:

- Keep the service deterministic for inventory and scoring decisions.
- Update `README.md`, `architecture.md`, or `roadmap.md` whenever implementation state, plans, or integration decisions change.
- Do not commit secrets; keep `.env` local and use `.env.example` for documented config keys only.
