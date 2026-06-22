# Architecture

## Overview

The system is built around Grocy as the source of truth for food inventory. A small Python service reads stock and recipe data, applies expiry-aware scoring, enriches suggestions with flavor-pairing intelligence, and serves its own single-page web app (the "fridge now" UI). The SPA is the primary surface: it shows urgency bands, runs voice/photo intake, suggests AI recipes, and writes changes (consume / toss / due-date edits, intake adds, cook consumption) straight back to Grocy. It can be embedded in Home Assistant as a webpage panel, but Home Assistant is an optional host, not a required output.

## Components

### Grocy

Stores:

- Products
- Stock entries
- Expiry dates
- Opened dates
- Shopping lists
- Recipes
- Meal plans

### FoodBrain Service

A custom Python service responsible for:

- Reading Grocy stock through the Grocy API
- Normalizing ingredient names
- Calculating expiry urgency
- Matching available stock against recipes
- Querying FlavorGraph embeddings
- Ranking meal suggestions
- Voice/photo intake and AI recipe inspiration via an LLM
- Writing changes back to Grocy (consume, toss, due-date edits, intake adds, cook consumption)
- Serving the SPA and a JSON API

Current implementation state:

- `foodbrain_assistant.config` loads environment configuration.
- `foodbrain_assistant.grocy_client` reads, parses, and diagnoses Grocy `/api/stock` responses with explicit parser tests, and provides the write primitives (consume/open/add-stock/create-product/undo). Static master data (locations, quantity units) is served from a short module-level TTL cache; stock and products are never cached so writes are reflected immediately.
- `foodbrain_assistant.writeback` is the safety layer over those writes (confirm-on-destructive, undoable outcomes).
- `foodbrain_assistant.scoring` ranks stocked ingredients by expiry urgency.
- `foodbrain_assistant.normalization` holds the shared name/token helpers (`normalize_ingredient_name`, `tokenize`, `tokens_match`, `str_list`, `blank_to_none`) used across matching, pairing, and the API.
- `foodbrain_assistant.recipes` loads recipes from local JSON files or from Grocy recipe objects (joining `recipes`, `recipes_pos`, and `products`), and parses ingredient lines into quantity, unit, and normalized name.
- `foodbrain_assistant.matching` matches recipes against stock and ranks them by pantry coverage and expiry usefulness.
- `foodbrain_assistant.llm` is the one tested OpenRouter (OpenAI-compatible) transport; `foodbrain_assistant.intake` (voice/photo capture → understand → reconcile) and `foodbrain_assistant.recipes_llm` (recipe ideas, recipe generation, cook-consumption estimation) build on it.
- `foodbrain_assistant.cookmemory` is the durable, stdlib-only learning store (taste, twists, anti-repeat log, saved-recipe book, cook sessions) written atomically and guarded by a lock; a corrupt file is backed up rather than overwritten.
- `foodbrain_assistant.api` is the transport-agnostic `FoodBrainAPI` (stock/connect/build-prompt + intake, recipe, and cook operations); `foodbrain_assistant.server` is the thin stdlib `http.server` transport that exposes the JSON API and serves the SPA (`prototype/fridge-now.html`) at `/ui`.
- `foodbrain_assistant.cli` provides `foodbrain --sample`, `foodbrain --grocy-stock-json`, `foodbrain --diagnose-grocy-stock-json`, `foodbrain --recipes-json`, `foodbrain --grocy-recipes-json`, `foodbrain --diagnose-grocy-recipes-json`, `foodbrain --grocy-recipes`, and `foodbrain` for live Grocy runs (diagnostics; the SPA/API is the day-to-day surface).

The first implementation intentionally has no runtime third-party dependencies so it can run on a small home server with only Python installed.

### FlavorGraph Layer

Provides:

- Ingredient similarity
- Pairing recommendations
- Creative ingredient substitutions
- Unexpected but plausible flavor combinations

The first implementation should use precomputed FlavorGraph embeddings instead of retraining the original model.

### Home Assistant (optional host)

Home Assistant is an optional embedding host: the FoodBrain SPA is served
same-origin at `/ui` and can be registered as a Home Assistant "Webpage"
dashboard so it appears in the HA sidebar. HA provides no data and receives no
push from FoodBrain; the old webhook publisher was removed. (The legacy
`panel_iframe` integration is gone from current HA — use the Webpage dashboard.)

### External Data Sources

Possible optional sources:

- Open Food Facts for barcode and product metadata
- USDA FoodData Central for nutrition
- FlavorDB for molecule-level flavor explanations
- recipe-scrapers for importing recipes
- ingredient-parser for structured ingredient parsing

## Data Flow

```text
Grocy Stock + Recipes  <--------------------+
        |                                   |
        v                                   | write-back
FoodBrain Service                           | (consume / toss / due-date,
        |                                   |  intake adds, cook consumption)
        |-- expiry scoring                  |
        |-- recipe matching                 |
        |-- FlavorGraph pairing             |
        |-- LLM intake + recipe inspiration |
        v                                   |
SPA (/ui) + JSON API  ----------------------+
        |
        |-- urgency bands, multi-select, editable prompt
        |-- voice/photo intake, AI recipes, cook tracking
        |
        v
(optional) embedded as a Home Assistant webpage panel
```

## Recommendation Scoring

A first scoring model:

```text
meal_score =
  expiry_urgency
+ pantry_coverage
+ flavor_pairing_score
+ user_preference_score
- missing_ingredient_penalty
- effort_penalty
```

## Design Principle

Inventory and expiry dates decide what matters. FlavorGraph makes the result more interesting.

## Implementation Notes

- Python compatibility target: 3.9 and newer.
- Live Grocy access requires `FOODBRAIN_GROCY_BASE_URL` and `FOODBRAIN_GROCY_API_KEY`.
- Saved Grocy `/api/stock` JSON can be diagnosed with `foodbrain --diagnose-grocy-stock-json` before running recommendations against it.
- Recipe matching is a deterministic heuristic: an ingredient and a stock item match when the word set of one contains the other's (after lowercasing and light singularization). `meal_score = coverage + 0.5 * expiry_usefulness`, where `expiry_usefulness` sums the urgency of matched stock so recipes that use up expiring items rank higher.
- Grocy recipes are a join across object endpoints rather than free text: `recipes_pos` rows reference a `product_id` and `amount`, resolved to names via `products` and to units via `quantity_units`. Internal meal-plan recipes (`type` other than `normal`) are skipped, and recipes with no resolvable ingredients are dropped rather than raising, mirroring how stock parsing tolerates bad rows. An exported bundle (`{recipes, recipes_pos, products, quantity_units}`) can be diagnosed with `foodbrain --diagnose-grocy-recipes-json` before matching.
- The SPA is served same-origin at `/ui` by `foodbrain_assistant.server`; it can be embedded as a Home Assistant Webpage dashboard. The earlier Home Assistant webhook publisher has been removed.
- LLM features (intake, recipe inspiration, cook estimation) go through OpenRouter (`FOODBRAIN_OPENROUTER_API_KEY`); calls are stateless and use `temperature:0` + `json_object`. The default model is `google/gemini-3.1-flash-lite`.
- `2999-12-31` from Grocy is treated as no practical expiry date.
