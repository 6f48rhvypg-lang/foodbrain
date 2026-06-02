# Architecture

## Overview

The system is built around Grocy as the source of truth for food inventory. A small recommendation service reads stock and recipe data, applies expiry-aware scoring, enriches suggestions with flavor-pairing intelligence, and publishes results to Home Assistant.

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
- Sending output to Home Assistant

Current implementation state:

- `foodbrain_assistant.config` loads environment configuration.
- `foodbrain_assistant.grocy_client` reads, parses, and diagnoses Grocy `/api/stock` responses with explicit parser tests.
- `foodbrain_assistant.scoring` ranks stocked ingredients by expiry urgency.
- `foodbrain_assistant.home_assistant` can publish a run summary to a webhook URL.
- `foodbrain_assistant.cli` provides `foodbrain --sample`, `foodbrain --grocy-stock-json`, `foodbrain --diagnose-grocy-stock-json`, and `foodbrain` for live Grocy runs.

The first implementation intentionally has no runtime third-party dependencies so it can run on a small home server with only Python installed.

### FlavorGraph Layer

Provides:

- Ingredient similarity
- Pairing recommendations
- Creative ingredient substitutions
- Unexpected but plausible flavor combinations

The first implementation should use precomputed FlavorGraph embeddings instead of retraining the original model.

### Home Assistant

Displays and automates:

- Daily meal suggestions
- Expiring ingredient alerts
- Shopping list gaps
- "Cook this today" notifications
- Dashboard cards

### External Data Sources

Possible optional sources:

- Open Food Facts for barcode and product metadata
- USDA FoodData Central for nutrition
- FlavorDB for molecule-level flavor explanations
- recipe-scrapers for importing recipes
- ingredient-parser for structured ingredient parsing

## Data Flow

```text
Grocy Stock + Recipes
        |
        v
FoodBrain Service
        |
        |-- expiry scoring
        |-- recipe matching
        |-- FlavorGraph pairing
        |-- shopping gap detection
        v
Home Assistant
        |
        |-- dashboard
        |-- notifications
        |-- automations
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
- Home Assistant publishing currently uses a webhook URL; MQTT is still planned.
- `2999-12-31` from Grocy is treated as no practical expiry date.
