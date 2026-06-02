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
- Expiry-aware ingredient urgency scoring
- Optional Home Assistant webhook publishing
- CLI for sample and live runs
- Unit tests for normalization and scoring
- Git repository initialized and pushed to `https://github.com/RSM-CEI/foodbrain`

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests
foodbrain --sample
```

For a live Grocy run, copy `.env.example` to `.env`, fill in the values, export them in your shell, then run:

```bash
foodbrain
```

Required environment variables for live Grocy access:

- `FOODBRAIN_GROCY_BASE_URL`
- `FOODBRAIN_GROCY_API_KEY`

Optional environment variables:

- `FOODBRAIN_HOME_ASSISTANT_WEBHOOK_URL`
- `FOODBRAIN_EXPIRY_WINDOW_DAYS`
- `FOODBRAIN_TOP_INGREDIENT_LIMIT`

## Current Development Plan

1. Verify the Grocy stock response shape against a real instance.
2. Add Home Assistant MQTT publishing if webhook automation is not enough.
3. Add recipe matching once stock ingestion is confirmed.
4. Add FlavorGraph embeddings after deterministic expiry and recipe scoring work.

## Next Session Handoff

Current repo state:

- GitHub remote: `https://github.com/RSM-CEI/foodbrain`
- Branch: `main`
- Last known clean state: local `main` should match `origin/main`
- Local verification command: `PYTHONPATH=src python3 -m unittest discover -s tests`
- Sample run command: `PYTHONPATH=src python3 -m foodbrain_assistant.cli --sample`

Start the next session by checking:

```bash
git status --short --branch
git pull --ff-only
PYTHONPATH=src python3 -m unittest discover -s tests
```

Next implementation steps:

1. Connect to a real Grocy instance by setting `FOODBRAIN_GROCY_BASE_URL` and `FOODBRAIN_GROCY_API_KEY`.
2. Run `foodbrain` against live stock and compare the parsed output with Grocy's visible inventory.
3. Adjust `foodbrain_assistant.grocy_client` if the real `/api/stock` response shape differs from the current parser assumptions.
4. Decide the Home Assistant integration path:
	- Keep webhook publishing if one daily summary is enough.
	- Add MQTT publishing if dashboard sensors/entities are needed.
5. After stock parsing is confirmed, begin Phase 4 recipe matching with local recipe fixtures before calling live recipe APIs.

Important constraints for the next session:

- Keep the service deterministic for inventory and scoring decisions.
- Update `README.md`, `architecture.md`, or `roadmap.md` whenever implementation state, plans, or integration decisions change.
- Do not commit secrets; keep `.env` local and use `.env.example` for documented config keys only.
