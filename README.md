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
- FlavorGraph-style ingredient pairing suggestions for soon-to-expire stock
- Optional Home Assistant webhook publishing
- CLI for sample and live runs
- Unit tests for Grocy parsing, normalization, scoring, recipe parsing, matching, and pairing
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

### Flavor pairings (Phase 5)

To suggest compatible flavor pairings for your soon-to-expire ingredients, pass
a pairings JSON file. The file is a list of undirected weighted pairs (or
`{"pairs": [...]}`), each with `a`, `b`, and an optional `score` (a 0..1 flavor
affinity). A sample lives at
[examples/pairings.sample.json](examples/pairings.sample.json):

```bash
foodbrain --sample --pairings-json examples/pairings.sample.json
```

Pairings are looked up for the most urgent ingredients first, and partners that
are also in stock are flagged so they are immediately actionable. This data is
the queryable form of FlavorGraph embeddings (top neighbors per ingredient);
the bundle can be regenerated offline from real FlavorGraph embeddings and
dropped in without code changes. The `--pairings-json` flag combines with any
stock and recipe source.

#### Generating a real FlavorGraph bundle

The sample is hand-authored. To build a bundle from the real FlavorGraph
embeddings, download the two public artifacts into `.foodbrain-local/flavorgraph/`
(both are gitignored) and run the offline generator:

```bash
# 1. node list (in the FlavorGraph repo)
curl -sSL -o .foodbrain-local/flavorgraph/nodes_191120.csv \
  https://raw.githubusercontent.com/lamypark/FlavorGraph/master/input/nodes_191120.csv
# 2. the 300D node-embedding pickle (~10MB, linked from the FlavorGraph README's
#    "Embeddings" section) -> save as
#    .foodbrain-local/flavorgraph/node_embeddings.pickle

# 3. build the bundle (numpy required; only used by this offline script)
PYTHONPATH=src python3 scripts/build_flavor_pairings.py --min-score 0.5

# 4. use it
foodbrain --sample --pairings-json .foodbrain-local/pairings.json
```

The generator keeps only `ingredient`-type nodes, computes each ingredient's
top-k cosine neighbors (`--top-k`, default 10), normalizes scores to 0..1, and
writes `{"pairs": [...]}`. The embeddings and the generated bundle are data, not
code -- keep them under `.foodbrain-local/` and do not commit them.

#### Non-English ingredient names (alias map)

FlavorGraph nodes and the sample recipes are English, but a live Grocy household
may name products in another language (Milch, Eier, ...). An alias map resolves
those names to the English vocabulary so recipe matching and flavor pairings
still fire. The map is a flat `{ "source": "target" }` JSON file of normalized
names; a German map of ~144 common grocery terms lives at
[examples/aliases.sample.json](examples/aliases.sample.json). Every target in it
was validated against the real FlavorGraph ingredient vocabulary, so each alias
resolves to an actual node.

The alias map loads automatically: `examples/aliases.sample.json` is used when
present, and an optional private `.foodbrain-local/aliases.json` (gitignored) is
layered on top for household-specific mappings. Pass `--aliases-json PATH` to use
a specific file instead. It combines with any stock, recipe, and pairing source.

```bash
foodbrain --grocy-stock-json .foodbrain-local/stock.json \
  --pairings-json .foodbrain-local/pairings.json \
  --aliases-json examples/aliases.sample.json
```

Aliasing is applied inside the shared `normalize_ingredient_name` chokepoint
(whole name first, then per token, before singularization), so one map fixes
both lookups. With no map, behavior is unchanged.

#### Grocy write-back (consume / toss / edit due date)

The service can write back to Grocy, not just read. `GrocyClient` is **read-only by
default**; construct it with `allow_writes=True` to enable the write primitives
(`consume_product`, `open_product`, `set_entry_due_date`, `undo_transaction`).
Any write on a read-only client raises `GrocyWriteDisabledError`, so tests and
dry-runs cannot mutate a live Grocy by accident.

The [writeback](src/foodbrain_assistant/writeback.py) module wraps those
primitives with the two safety rails from the UX design:

- **Confirm on destructive** — `writeback.toss(...)` raises `ConfirmationRequired`
  unless called with `confirm=True`.
- **Undo on consume** — `writeback.consume(...)` / `toss(...)` return a
  `WriteOutcome` carrying the Grocy `transaction_id`; `writeback.undo(client,
  outcome)` reverses it.

Editing a due date targets an individual stock entry: read
`client.get_product_entries(product_id)` to get entry ids, then call
`writeback.set_due_date(client, entry_id, date(...))`.

This is the backend muscle (build order step 1) behind the planned JSON API and
SPA; see [ux-design.md](ux-design.md).

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
- `FOODBRAIN_TOP_PAIRING_LIMIT`
- `FOODBRAIN_PAIRING_PARTNER_LIMIT`

#### JSON API (build order step 2)

The recommendation engine and write-back rails are exposed as a small JSON API
for the planned SPA. The runtime stays dependency-free: it is served by the
stdlib `http.server`. The logic itself lives in a transport-agnostic
[`FoodBrainAPI`](src/foodbrain_assistant/api.py); [`server.py`](src/foodbrain_assistant/server.py)
is the thin HTTP wrapper.

Run it against the built-in sample stock (no Grocy needed):

```bash
PYTHONPATH=src python3 -m foodbrain_assistant.server --sample \
  --pairings-json examples/pairings.sample.json \
  --recipes-json examples/recipes.sample.json
```

Or against live Grocy (reads from `/api/stock`; writes need a configured `.env`):

```bash
PYTHONPATH=src python3 -m foodbrain_assistant.server   # add --stock-json PATH to serve an export
```

Routes (default `http://127.0.0.1:8123`):

| Method & path | Purpose |
| --- | --- |
| `GET /api/health` | Liveness check. |
| `GET /api/stock` | Bands view: every item with urgency score + band (`hot`/`warm`/`cool`/`staple`). |
| `POST /api/connect` | `{"selection": [product_id, ...]}` → flavor pairings among the selection + the recipes it unlocks. |
| `POST /api/build-prompt` | `{"selection": [...]}` → an editable LLM prompt (no LLM call; the SPA copies it). |
| `GET /api/product-entries?product_id=ID` | A product's stock entries (for the edit-date flow). |
| `POST /api/consume` | `{"product_id": ID, "amount": 1}` → undoable consume. |
| `POST /api/toss` | `{"product_id": ID, "amount": 1, "confirm": true}` → destructive; `409` without `confirm`. |
| `POST /api/set-due-date` | `{"stock_entry_id": ID, "best_before_date": "YYYY-MM-DD"}`. |
| `POST /api/undo` | `{"transaction_id": ID}` → reverse a consume/toss. |

Writes are disabled (`403`) unless `FOODBRAIN_GROCY_BASE_URL` and
`FOODBRAIN_GROCY_API_KEY` are configured; the API then builds a writable
`GrocyClient` only for write/entry calls. CORS is permissive so the SPA can be
developed from a separate dev origin.

#### SPA (build order step 3)

[`prototype/fridge-now.html`](prototype/fridge-now.html) is the single-file SPA —
the urgency-bands fridge view with multi-select, the Connect/Ask action bar, the
editable prompt box, and inline quick actions (consume / toss / edit due date)
with an undo snackbar. It is now wired to the JSON API above (no more mock data):

- `GET /api/stock` populates the bands; `band` and `as_of` come straight from the
  API so the grouping always matches the engine.
- `POST /api/connect` and `POST /api/build-prompt` back the action bar.
- `POST /api/consume` / `/api/toss` / `/api/set-due-date` (via
  `GET /api/product-entries`) back the inline actions; `POST /api/undo` backs the
  snackbar using the `transaction_id` the write returns.

Start the API (see above), then open the file. Over `file://` it defaults to
`http://127.0.0.1:8123`; when served from the FoodBrain server it uses the same
origin. Point it elsewhere with `?api=http://host:port`. Writes hit the live
Grocy LXC, so they need a configured `.env` and otherwise surface the API error
in the snackbar. Step 4 will embed this as a Home Assistant webpage panel.

## Current Development Plan

1. Stock ingestion is confirmed against real Grocy data (Phase 3 done).
2. Recipe matching is implemented for local files and Grocy recipes (Phase 4).
3. Grocy recipe matching is verified against the live household instance (which
   currently has products but no recipes defined yet).
4. FlavorGraph pairing suggestions are implemented from a local pairings bundle
   (Phase 5) and can be generated from the real FlavorGraph embeddings via
   `scripts/build_flavor_pairings.py`. Remaining options: add Home Assistant MQTT,
   add Mealie/Tandoor recipe sources, or add an English/German alias map so live
   Grocy product names resolve against the (English) FlavorGraph nodes.

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
