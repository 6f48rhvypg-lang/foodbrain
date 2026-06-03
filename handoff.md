# FoodBrain Handoff

Current date: 2026-06-03

## Start Here Next

The **real FlavorGraph pairings bundle generator is done** (added
`scripts/build_flavor_pairings.py`). It loads the public FlavorGraph node list +
300D embedding pickle, filters to ingredient nodes, computes top-k cosine
neighbors, and writes the `{"pairs": [...]}` bundle the runtime already consumes.
It was run and verified end to end against the sample stock on this machine:
6653 ingredient nodes -> 48,227 pairs at `--min-score 0.5`, and real pairings
resolve through the token matcher (e.g. Rice -> "frozen peas and carrot (in
stock)"). The artifacts and generated bundle live only under `.foodbrain-local/`
(gitignored); only the script is committed.

The single recommended next task is to **add an English/German alias map** so
live Grocy product names (Milch, Eier, …) resolve against the English FlavorGraph
nodes — otherwise live pairings under-match. See "Next Implementation Decision".

First commands on a fresh machine:

```bash
git pull --ff-only
PYTHONPATH=src python3 -m unittest discover -s tests   # expect: Ran 52 tests, OK (skipped=1)
PYTHONPATH=src python3 -m foodbrain_assistant.cli --sample --pairings-json examples/pairings.sample.json
# Optional: rebuild the real bundle (see "Generating a real FlavorGraph bundle" in README)
PYTHONPATH=src python3 scripts/build_flavor_pairings.py --min-score 0.5
PYTHONPATH=src python3 -m foodbrain_assistant.cli --sample --pairings-json .foodbrain-local/pairings.json
```

## Current State

- Repository: `https://github.com/RSM-CEI/foodbrain`
- Branch: `main` (pushed; HEAD `99acbf9` "Add Phase 5 FlavorGraph ingredient pairing")
- The Python sidecar service can read Grocy stock, score expiry urgency, match local recipes against stock, print CLI output, and optionally publish a Home Assistant webhook summary.
- Grocy `/api/stock` parsing now has unit tests, an automatic diagnostics command, and an optional real-data contract test.
- Local household data exports should be saved under `.foodbrain-local/`, which is ignored by git.
- Live Grocy credentials can be stored in a local ignored `.env` file copied from `.env.example`.
- LIVE-VERIFIED (2026-06-02): Confirmed against a real self-hosted Grocy instance
  (Proxmox LXC at `http://192.168.178.150`). `scripts/fetch_grocy_stock.py` pulled
  the live `/api/stock`, diagnostics passed with no errors/warnings, the contract
  test passed, and `foodbrain` produced a real recommendation. Roadmap Phase 3
  "Verify Grocy response parsing against real household data" is now complete.
- LIVE-VERIFIED RECIPES (2026-06-02): `scripts/fetch_grocy_recipes.py`,
  `--diagnose-grocy-recipes-json`, and `--grocy-recipes` all ran successfully
  against the live instance (exit 0, no errors/warnings, real recommendation
  produced). FINDING: the live Grocy currently has **zero recipes defined**
  (`recipes: []`, `recipes_pos: []`); only products (Eier, Milch) and quantity
  units (Piece, Pack) exist. The recipe-source code path is verified to handle
  real data and degrade gracefully, but the matching heuristic itself remains
  unverified against real recipes until some are added to Grocy.
- The real Grocy base URL and API key live only in the local ignored `.env`; they
  are not committed. Re-create `.env` from `.env.example` on each machine.

## Changed In This Session

### Real FlavorGraph bundle generator (latest)

- Added `scripts/build_flavor_pairings.py`: an offline, one-off generator that
  turns the real FlavorGraph artifacts into the runtime `{"pairs": [...]}` bundle.
  - Inputs (download into `.foodbrain-local/flavorgraph/`, both gitignored):
    `nodes_191120.csv` (from `lamypark/FlavorGraph` repo, `input/`) and the 300D
    node-embedding pickle (~10MB Google Drive link in the FlavorGraph README's
    "Embeddings" section), saved as `node_embeddings.pickle`.
  - The pickle is a `{node_id (str): numpy.float32[300]}` dict (8297 nodes). The
    CSV `node_type` column separates `ingredient` (6653) from flavor-compound
    nodes; only ingredient nodes are kept.
  - numpy is used **only in the script** (offline); the package runtime stays
    dependency-free. Cosine similarity is computed block-wise; scores clamped to
    0..1; output deterministic and deduped to undirected pairs (max score).
  - Flags: `--nodes-csv`, `--embeddings`, `--out` (default
    `.foodbrain-local/pairings.json`), `--top-k` (default 10), `--min-score`
    (default 0.0).
  - VERIFIED on this machine: produced 48,227 pairs at `--min-score 0.5` and ran
    end to end via `foodbrain --sample --pairings-json .foodbrain-local/pairings.json`.
    Real names resolve through the existing token matcher and the in-stock flag
    fires. Test suite still 52 (1 skipped); only the script is committed (no data).
- README gained a "Generating a real FlavorGraph bundle" subsection with the
  download + run commands.
- KNOWN LIMITATION: FlavorGraph nodes are English; live Grocy products are German
  (Milch, Eier). Live pairings will under-match until an alias map is added — the
  recommended next task.

### Phase 5 FlavorGraph pairing (earlier)

- Added `foodbrain_assistant.pairing`: `load_pairings` builds a symmetric
  `PairingGraph` from a local JSON bundle (`{"pairs": [{"a","b","score"}]}` or a
  bare list); `suggest_pairings` produces pairing suggestions for the most urgent
  stock ingredients and flags partners that are also in stock.
- Lookup mirrors `matching`'s explainable token-containment + singularization
  heuristic, so "Greek yogurt" resolves to the "yogurt" node and "Carrots" to
  "carrot". Pairing is offline-first and dependency-free; the bundle is the
  queryable form of FlavorGraph embeddings (top neighbors per ingredient) and can
  be regenerated from real embeddings without code changes.
- Added `FlavorPartner` and `FlavorSuggestion` models; `RunResult` now carries
  `flavor_suggestions`.
- Added CLI flag `--pairings-json PATH`; suggestions appear in text and JSON
  output and in the Home Assistant webhook payload. Works with any stock/recipe
  source.
- Added `FOODBRAIN_TOP_PAIRING_LIMIT` (default 5, ingredients suggested for) and
  `FOODBRAIN_PAIRING_PARTNER_LIMIT` (default 4, partners per ingredient) to
  `config.Settings`, `.env.example`, and README.
- Added `examples/pairings.sample.json` and `tests/test_pairing.py`. Test suite
  is now 52 tests (1 skipped).
- NOT yet backed by a real FlavorGraph embeddings bundle — sample pairings are
  hand-authored. Generating a real bundle from FlavorGraph embeddings is the
  obvious next refinement.

### Grocy recipe source (earlier this session)

- Added a live Grocy recipe source: `parse_grocy_recipes_response` and `diagnose_grocy_recipes` in `foodbrain_assistant.recipes` join the `recipes`, `recipes_pos`, `products`, and `quantity_units` object endpoints into the same `Recipe` model used for local files.
- Internal meal-plan recipes (`type` != `normal`) are skipped; recipes with no resolvable ingredients are dropped rather than raising, mirroring stock parsing tolerance.
- Added `GrocyClient.get_recipes()` (fetches the four object endpoints and joins them).
- Added CLI flags: `--grocy-recipes-json PATH` (match an exported bundle), `--diagnose-grocy-recipes-json PATH` (validate a bundle, non-zero exit on hard errors), and `--grocy-recipes` (fetch live from Grocy). The three recipe sources are mutually exclusive.
- Added `scripts/fetch_grocy_recipes.py` to export the recipe bundle to `.foodbrain-local/recipes.json` without committing data.
- Added `tests/test_grocy_recipes.py`. Test suite is now 37 tests (1 skipped).
- NOT yet verified against real Grocy recipe data — that is the next step (see below).

### Phase 4 local recipe matching (earlier this session)

- Started Phase 4 recipe matching with local recipe fixtures (the recommended next step from the previous handoff).
- Added `foodbrain_assistant.recipes`: loads recipes from local JSON files and parses ingredient lines into quantity, unit, and normalized name.
- Added `foodbrain_assistant.models.Recipe`, `RecipeIngredient`, and `RecipeMatch`; `RunResult` now carries `recipe_matches`.
- Added `foodbrain_assistant.matching`: deterministic recipe-to-stock matching ranked by `coverage + 0.5 * expiry_usefulness`.
- Added `foodbrain --recipes-json PATH`, which works with `--sample`, `--grocy-stock-json`, and live Grocy runs; recipe matches are shown in text and JSON output and included in the Home Assistant webhook payload.
- Added `FOODBRAIN_TOP_RECIPE_LIMIT` (default 5) to `config.Settings`, `.env.example`, and README.
- Added `examples/recipes.sample.json` and tests `tests/test_recipes.py` and `tests/test_matching.py`.

### Phase 3 (prior session)

- Added `parse_stock_response` coverage for common Grocy stock response shapes.
- Added `diagnose_stock_response` to summarize real Grocy payload compatibility.
- Added `foodbrain --grocy-stock-json` for running recommendations against an exported Grocy stock JSON file.
- Added `foodbrain --diagnose-grocy-stock-json` for validating an exported stock JSON file before using it.
- Added `tests/test_grocy_real_stock_contract.py`, which runs only when `FOODBRAIN_GROCY_STOCK_JSON` points to a local export.
- Added `.foodbrain-local/` to `.gitignore` for private local exports.
- Added dependency-free `.env` loading so live Grocy config can be kept in a local ignored file.

## Verification Commands

Run the normal test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Expected result at handoff:

```text
Ran 52 tests
OK (skipped=1)
```

Run recipe matching and flavor pairings against the sample stock:

```bash
PYTHONPATH=src python3 -m foodbrain_assistant.cli --sample \
  --recipes-json examples/recipes.sample.json \
  --pairings-json examples/pairings.sample.json
```

Export, diagnose, and match Grocy recipes (needs a configured `.env`):

```bash
PYTHONPATH=src python3 scripts/fetch_grocy_recipes.py
PYTHONPATH=src python3 -m foodbrain_assistant.cli --diagnose-grocy-recipes-json .foodbrain-local/recipes.json
PYTHONPATH=src python3 -m foodbrain_assistant.cli --grocy-recipes
```

Run the sample CLI flow:

```bash
PYTHONPATH=src python3 -m foodbrain_assistant.cli --sample --json
```

Run diagnostics against a local Grocy export:

```bash
mkdir -p .foodbrain-local
PYTHONPATH=src python3 -m foodbrain_assistant.cli --diagnose-grocy-stock-json .foodbrain-local/stock.json
```

Run the optional real-data parser contract test:

```bash
FOODBRAIN_GROCY_STOCK_JSON=.foodbrain-local/stock.json PYTHONPATH=src python3 -m unittest tests.test_grocy_real_stock_contract
```

## Next Session Start

1. Pull the latest code on the other PC:

```bash
git clone https://github.com/RSM-CEI/foodbrain.git
cd foodbrain
```

If the repo already exists there:

```bash
cd foodbrain
git pull --ff-only
```

2. Create local Grocy configuration from the example:

```bash
cp .env.example .env
```

Then edit `.env` and fill in:

```bash
FOODBRAIN_GROCY_BASE_URL=...
FOODBRAIN_GROCY_API_KEY=...
```

Do not commit `.env`.

3. Check repository state:

```bash
git status --short --branch
```

4. Run the normal tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

5. Run the live Grocy flow:

```bash
PYTHONPATH=src python3 -m foodbrain_assistant.cli
```

6. If you save a real Grocy export, run diagnostics:

```bash
mkdir -p .foodbrain-local
PYTHONPATH=src python3 -m foodbrain_assistant.cli --diagnose-grocy-stock-json .foodbrain-local/stock.json
```

## Next Implementation Decision

Phase 4 recipe matching is implemented and verified live (the household Grocy has
products but no recipes yet — see LIVE-VERIFIED RECIPES above). Phase 5
FlavorGraph pairing is implemented from a local pairings bundle, and the **real
FlavorGraph bundle generator now exists and is verified** (see "Real FlavorGraph
bundle generator" above). The data-generation task is therefore DONE.

The immediate next refinement is an **English/German alias map** so live Grocy
product names resolve against the (English) FlavorGraph nodes. Today, `--sample`
resolves well because the sample stock is English; live stock (Milch, Eier) will
under-match until aliases exist.

Concrete plan for the next session:

1. Add a small alias map (e.g. `Milch -> milk`, `Eier -> egg`, `Käse -> cheese`)
   used by the pairing (and ideally matching) lookup. Keep it data-driven and
   offline; a JSON file under `examples/` plus an optional local override is in
   keeping with the rest of FoodBrain.
2. Apply aliases in `foodbrain_assistant.pairing` (which mirrors `matching`'s
   token heuristic) before normalization, so "Milch" tokenizes as "milk".
3. Verify against live stock (needs a configured `.env` and the real bundle):

```bash
PYTHONPATH=src python3 scripts/build_flavor_pairings.py --min-score 0.5
PYTHONPATH=src python3 -m foodbrain_assistant.cli --grocy-stock-json .foodbrain-local/stock.json \
  --pairings-json .foodbrain-local/pairings.json
```

To regenerate the bundle, re-download the two FlavorGraph artifacts into
`.foodbrain-local/flavorgraph/` (see README "Generating a real FlavorGraph
bundle") and re-run `scripts/build_flavor_pairings.py`. The generated bundle and
the embeddings stay under `.foodbrain-local/` (gitignored); never commit them.

Other open options:

- Verify recipe + pairing matching against real recipes once some are added to
  the household Grocy.
- Home Assistant MQTT — only if a live dashboard is wanted; the webhook summary
  already carries recipe matches and flavor suggestions.
- Optional: Mealie or Tandoor as additional recipe sources.

To re-run the live verification on any machine that can reach the Grocy LXC:

```bash
cp .env.example .env   # then fill in the real URL + API key
PYTHONPATH=src python3 scripts/fetch_grocy_stock.py
PYTHONPATH=src python3 -m foodbrain_assistant.cli --diagnose-grocy-stock-json .foodbrain-local/stock.json
PYTHONPATH=src python3 -m foodbrain_assistant.cli
```

Keep inventory parsing and scoring deterministic. Do not commit `.env`, API keys, or `.foodbrain-local/` contents.