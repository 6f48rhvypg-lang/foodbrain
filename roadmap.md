# Roadmap

## Phase 1: Foundation

- Choose project name
- Set up Grocy
- Add common pantry/fridge/freezer products
- Enable barcode lookup with Open Food Facts
- Track expiry dates for real groceries
- Connect Grocy to Home Assistant

## Phase 2: Visibility

- Create Home Assistant sensors for:
  - expiring products
  - overdue products
  - missing products
  - shopping list items
- Build a basic dashboard
- Add a daily notification for urgent ingredients

## Phase 3: Recommendation Service

Current handoff: see [handoff.md](handoff.md) for restart commands, verification status, and the next implementation decision.

- [x] Create a Python sidecar service
- [x] Read stock from Grocy API
- [x] Rank ingredients by expiry urgency
- [x] Output top ingredients to use today
- [x] Publish results to Home Assistant via REST webhook
- [x] Initialize git repository and push to GitHub
- [x] Add saved Grocy stock response parser diagnostic workflow
- [x] Verify Grocy response parsing against real household data
- [ ] Decide whether MQTT is needed in addition to webhooks

## Phase 4: Recipe Matching

- [x] Read recipes from local JSON files (Grocy/Mealie/Tandoor sources still to come)
- [x] Parse ingredient lines (quantity, unit, name)
- [x] Normalize ingredient names
- [x] Match recipes against available stock
- [x] Rank recipes by pantry coverage and expiry usefulness
- [ ] Add a live recipe source (Grocy `/api/objects/recipes`, Mealie, or Tandoor)

## Phase 5: FlavorGraph Integration

- Download precomputed FlavorGraph embeddings
- Map Grocy ingredient names to FlavorGraph nodes
- Suggest compatible ingredients
- Add flavor-pairing score to recommendations
- Surface "try this pairing" suggestions

## Phase 6: Shopping Intelligence

- Detect small missing ingredients that unlock high-value meals
- Add suggested missing items to Grocy shopping list
- Prefer items that combine with multiple pantry ingredients
- Avoid recommending purchases that create more waste

## Phase 7: Personalization

- Track accepted and rejected suggestions
- Add cuisine preferences
- Add effort levels
- Add dietary constraints
- Add "quick dinner", "use leftovers", and "creative mode"

## Phase 8: Optional AI Layer

- Use a local LLM through Ollama or similar
- Generate cooking instructions from selected ingredients
- Keep inventory scoring deterministic
- Use AI only for wording, recipe adaptation, and explanation
