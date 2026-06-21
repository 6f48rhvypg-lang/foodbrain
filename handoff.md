# FoodBrain Handoff

Current date: 2026-06-12

---

## ✅ DONE (2026-06-21): Cook → consumption tracking + adjustable Verlauf

**Shipped.** "Gekocht ✓" now books consumption to Grocy via an editable estimate
review, keeps a correctable **Verlauf**, and surfaces **📖 Rezepte** at top level.

- `recipes_llm.estimate_consumption` (mirrors `generate_recipe`; injectable
  transport; normalizes `{used,bought}`, degrades to []; optional `correction`
  for spoken/typed re-estimate).
- `api.py`: injectable `consumption_estimator`; `recipe_cook_estimate`
  (reconcile_items → intake-shaped `kind:"consume"`/`"bought"` rows),
  `recipe_cook_commit` (consume rows → writeback.consume; bought rows → create/
  reuse via `_ProductIndex`+`_NameResolver`, `add_stock(pack)` then `consume(used)`
  → leftover stays; per-item failure isolation; `depleted` via `_live_stock_amount`
  ≤0; folds in `add_cooked` anti-repeat; persists `cookmemory.add_session`),
  `cook_history`, `cook_adjust` (undo old txn → reconsume new → recompute depleted
  → `update_session_line`).
- `cookmemory.py`: `sessions` in skeleton/normalize; `add_session`/`sessions`
  (newest-first)/`update_session_line`.
- `server.py`: `POST /api/recipes/cook-estimate|cook-commit|cook-adjust`,
  `GET /api/recipes/cook-history` (kept `/cooked` for back-compat).
- `prototype/fridge-now.html`: `openCookReview` (editable Menge consume rows +
  "Packung/benutzt/Rest" bought rows + mic re-estimate borrowing #intakeText/
  #micBtn) → grouped result (Hinzugefügt/Verbraucht/Ganz aufgebraucht ✓); Verlauf
  inside Einstellungen (`openCookHistory`, per-line amount → cook-adjust);
  top-level 📖 Rezepte button. Bonus: guarded a pre-existing `askBtn` null
  (removed in V4 redesign) that threw on every load.
- Tests: `tests/test_cook.py` (estimate reconcile, add+deduct-pack leftover,
  depleted flag, failure isolation, history order, adjust undo+reconsume, HTTP
  route flow), estimate normalization in `test_recipes_llm.py`, sessions in
  `test_cookmemory.py`. Suite **196 (1 skipped)**, all green.
- VERIFIED: SPA boots clean (no console errors), 📖 Rezepte + Verlauf render in a
  headless browser. The full LLM estimate→commit path (real OpenRouter + Grocy
  writes) was NOT auto-triggered — exercised via unit/HTTP tests with a fake
  estimator + mutating fake Grocy. Walk it live on the phone after deploy.

(Deploy: merge→main→push→`pct exec 105 -- git -C /opt/foodbrain pull` + restart.)

---

## 📋 ORIGINAL PLAN (2026-06-21): Cook → consumption tracking + adjustable Verlauf

**Goal.** Tapping **"Gekocht ✓"** on a recommended recipe should *book the
consumption* to Grocy (estimated), show an editable added/consumed/used-up
review, allow speech + manual adjustment, and keep a persistent **Verlauf**
(hidden in the recipe settings sheet) where past estimates stay correctable.
Plus a bonus fix: surface the saved-recipes book ("Meine Rezepte") at top level
(it's currently buried in the 🎨 color-theme popover — that's why a saved recipe
felt "lost"; it was saved fine).

**Confirmed product decisions.**
- New groceries (shop-mode `buy` items not yet in stock): **add the full pack to
  Grocy, then deduct the estimated used amount** → leftover stays in the fridge.
- Verlauf lives **hidden inside the recipe Einstellungen sheet**, not top level.
- Book gets a **top-level entry point**.

**Approach — reuse the intake review→reconcile→commit machinery; don't reinvent.**

Backend:
1. `recipes_llm.py` — new `estimate_consumption` mirroring `generate_recipe`
   (injectable `transport`, explicit `model` = cheap `recipe_model`, defensive
   normalization). In: dish, guidance, mode, in-stock candidates (name+amount+
   unit), shop `buy` list. Out (normalized, degrade to []):
   `{"used":[{name,amount,unit}], "bought":[{name,pack_amount,used_amount,unit}]}`.
   Model refers to items by **name only**; id resolution happens in the API.
2. `api.py` (`FoodBrainAPI`) — add injectable `consumption_estimator`, plus:
   - `recipe_cook_estimate(dish, guidance, buy, mode)`: pull stock via
     `stock_provider`, call estimator, resolve `used` names → products with
     **`reconcile_items`** (intake.py, already used by `intake_understand`),
     return intake-shaped rows (`kind:"consume"` / `kind:"bought"`).
   - `recipe_cook_commit(dish, items)`: per-item resilience like `intake_commit`
     (failures → `failed[]`). consume rows → `writeback.consume`; bought rows →
     resolve/create via existing **`_ProductIndex`+`_NameResolver`** (as
     `_commit_add`), `add_stock(pack)` then `consume(used)`. "Used up" =
     `_live_stock_amount` ≤ 0 after (writeback.py:40). Fold in old `add_cooked`
     anti-repeat. Persist via `cookmemory.add_session` storing each line's
     `product_id`/`amount`/`transaction_id`/`depleted`/`kind`. Return grouped
     `{added, consumed, failed, session_id}`.
   - `cook_history()` → `cookmemory.sessions`.
   - `cook_adjust(session_id, line_index, new_amount)`: `undo(txn)` → if >0
     `consume(pid,new)` → recompute depleted → `update_session_line`
     (reuses `writeback.undo`/`consume`).
3. `cookmemory.py` — add `"sessions":[]` to `_skeleton()`/`_normalize()`;
   `add_session`, `sessions` (newest-first like `book`), `update_session_line`.
4. `server.py` — routes mirroring existing recipe routes:
   `POST /api/recipes/cook-estimate|cook-commit|cook-adjust`,
   `GET /api/recipes/cook-history`. Leave `/api/recipes/cooked` for back-compat.

Frontend (`prototype/fridge-now.html`):
- `renderRecipe` (~L2201): **Gekocht ✓** → `openCookReview(rec, mode)` instead of
  the bare `cooked` POST.
- `openCookReview`: POST cook-estimate → review reusing **`editRow`/`renderReview`**
  (~L2620): editable Menge per consume row; bought rows show "+Pack · −benutzt ·
  Rest"; per-row manual edit + borrow the `#intakeText`/`#micBtn` speech engine
  (as `openTwist` does) to **re-estimate from a spoken correction** = speech
  adjust. Commit → cook-commit → grouped result: **Hinzugefügt / Verbraucht /
  Ganz aufgebraucht ✓**.
- Verlauf: add entry inside `openRecipeSettings` (~L2267) → `openCookHistory()`
  listing sessions; each consumed line gets a manual amount input → cook-adjust.
- Book: add a top-level 📖 **Meine Rezepte** affordance near the inspiration
  button calling existing `openBook()` (keep the palette-menu entry too).

Tests: `test_recipes_llm` (estimate normalization), `test_api` (reconcile,
add+deduct-pack leaves leftover, depleted flag, failure isolation, adjust
undo+reconsume, history order) with a fake estimator + fake `GrocyClient`;
`test_cookmemory` sessions round-trip.

Verify: `python -m pytest`; run SPA on sample/JSON stock, walk the cook flow
(estimate → edit → mic re-estimate → commit → grouped result), adjust a line in
Einstellungen→Verlauf, confirm 📖 top-level. Then merge→main→push→deploy CT 105
(`pct exec 105 -- git -C /opt/foodbrain pull && systemctl restart foodbrain`;
`curl localhost:8123/api/health`).

(Full plan also at `~/.claude/plans/ticklish-meandering-walrus.md`.)

---

## ✅ FIXED & DEPLOYED (2026-06-12): date-edit HTTP 400

**Was:** editing an item's due date in the SPA always failed with
"Grocy request failed with HTTP 400". Root cause: `GrocyClient.set_entry_due_date`
PUT a date-only body, but Grocy 4.6 requires `amount` in
`PUT /stock/entry/{id}` and replaces the whole row with what's sent.

**Fix (commit `046cbb9`):** read–merge–write in `set_entry_due_date`
(`src/foodbrain_assistant/grocy_client.py`) — write-guard first (read-only
client refuses before any request), then `GET /stock/entry/{id}`, then PUT the
full body: `amount` echoed, new `best_before_date`, `open` via
`_parse_amount(...) > 0` (handles Grocy's "0"/"1" strings), and
`price`/`location_id`/`shopping_location_id`/`purchased_date` echoed only when
not None. No changes were needed in writeback.py / api.py / server.py /
frontend; the voice-intake re-date path shares `writeback.set_due_date` and is
fixed for free.

**Tests:** `test_set_due_date_builds_put` rewritten to assert the GET+PUT pair
and that the PUT body carries `amount` (the regression), the new date,
`open: false`, preserved `location_id`/`purchased_date`, and no `price` key.
Suite **143 (1 skipped)**, all green.

**VERIFIED live, net-zero** (Gouda, product 80, stock entry 101): POST
`/api/set-due-date` 2026-06-26 → 2026-07-03 returned 200 where it used to 400;
raw Grocy row confirmed the date moved and amount/price/open/location_id/
purchased_date/stock_id all unchanged; then restored to 2026-06-26 and
re-confirmed. CT 105 deployed (`git pull` + restart, health ok).

## ✅ Voice EDIT mode + UX polish (2026-06-06, latest)

Voice now does two things, chosen from two big buttons at the top of the SPA:
**🛒 Add** (after shopping → stock) and **✏️ Edit** (used / wrong input →
consume / toss / fix date). Both run the same talk→understand→review→commit
pipeline; the button just sets a `mode` (`add`|`edit`) that swaps the model's
system prompt and the review sheet.

- `intake.py`: `IntakeItem.action` (`add`/`consume`/`toss`/`set_date`), a second
  `_EDIT_SYSTEM_PROMPT`, `understand_transcript(..., mode=)`, and action-synonym
  normalization (`finished`→consume, `threw away`→toss, …).
- `api.py`: `intake_understand(..., mode=)`; `intake_commit` now dispatches per
  item action — add (create/add stock), consume, toss (confirm+undo), set_date
  (resolves the first stock entry). Edit actions require a matched product or
  400. Returns `added`/`created_products`/`changed`.
- `server.py`: passes `mode` through to understand.
- `fridge-now.html`: two-button intake bar; edit review with per-row action chips
  (used/toss/date) + amount/date fields and a "not in fridge" guard; **smaller
  item name font (18→15px)** so long German names fit; a **food emoji** in front
  of each item (and each review row) via `emojiFor(name)`.
- Tests: +9 in `tests/test_intake.py` (edit prompt, synonyms, consume/toss/
  set_date commit, mixed batch, unmatched-edit reject). Suite **127 (1 skipped)**.
- VERIFIED live: edit-mode understand matched real Grocy products ("milk"→Milch
  consume, "two eggs"→Eier consume qty 2); a consume(0.5 Milch)→undo round-trip
  was **net-zero** against real Grocy. SPA screenshotted at phone width, console
  clean.


## ✅ COMPLETE (2026-06-06): Voice intake — deployed, HTTPS live, keys rotated

**DEPLOYED AND LIVE.** CT 105 is on `66b0a75`, `foodbrain.service` active,
`GET /api/health` → `intake_enabled: true`. **HTTPS via Tailscale** is up:
`/dev/net/tun` added to `/etc/pve/lxc/105.conf`, container rebooted, Tailscale
installed (`tailscale serve --bg 8123`). Phone URL:
**`https://foodbrain.tailcf4b7e.ts.net/ui`** (tailnet `tailcf4b7e`, CT 105 IP
`100.117.235.50`). Verified over the real cert: health + `/ui` + a live
`/api/intake/understand` parse ("half a liter of milk and three carrots" →
matched **Milch** exact, **Carrot** new).

**Keys rotated** (2026-06-06): new Grocy + OpenRouter keys generated, propagated
to both `.env` files (dev box + CT 105), and verified live (stock read 200,
intake parse 200). NOTE: the rotated OpenRouter key was first pasted with a
stray trailing `#` — removed; OpenRouter keys are `sk-or-v1-` + 64 hex, and the
dotenv loader keeps inline `#` literally (only whole-line `#` = comment).

**CT 104 stray `tailscale serve` cleaned up.** Remaining optional: point the HA
"Webpage" dashboard at the https URL; build the photo-intake next slice.

**Light-overhaul redesign deployed (`a1c1fe5`)** and confirmed working on the
phone (Safari direct to `…/ts.net/ui`). CT 105 is now on `a1c1fe5`. To update CT
105 in future: `pct exec 105 -- git -C /opt/foodbrain pull && pct exec 105 --
systemctl restart foodbrain` (driven from the dev box over
`ssh root@192.168.178.100`). **Caching gotcha:** the server sends no cache
headers for `/ui`, so Safari/PWA serve a stale copy after a deploy — bust it with
`…/ui?v=N` or clear Website Data. Open follow-up: make `/ui` send no-cache (or
version assets) so phones always get the latest.

The original step-by-step (now done) is preserved below for reference.

### ORIGINAL TASK (2026-06-06): Voice intake — built & pushed, NOT yet deployed

**The user's goal:** stop filling Grocy by hand. Instead, stand at the fridge,
**talk into the phone** ("half a liter of milk, opened, good for ~4 days, and
three carrots"), have an LLM parse it, ask clarifying questions, and write it
into Grocy. (Also wants a photo path later — snap a shelf, get every product.
That's the planned next slice; this session did the **voice-only slice**.)

### What is DONE (code complete, tested, pushed to `main`)

Commit **`66b0a75`** on `main` (pushed to `github.com/6f48rhvypg-lang/foodbrain`).
All 118 tests pass (`PYTHONPATH=src python -m unittest discover -s tests`).

New pipeline: **capture → understand → reconcile → review → commit**
- `src/foodbrain_assistant/intake.py` — OpenRouter (OpenAI-compatible) client
  (stdlib `urllib`, injectable `transport` for tests), strict-JSON prompt/parse,
  product reconciliation against existing Grocy products via the shared
  `normalize_ingredient_name` + alias rules (exact match, plus unambiguous
  containment "fuzzy"; ambiguous → left as `new`).
- `grocy_client.py` — `get_products`, `get_quantity_units`, `get_locations`,
  `create_product`, `add_stock` (purchase; undoable).
- `api.py` — `intake_understand` / `intake_commit` + `_NameResolver` (maps a
  free-text unit/location to a Grocy id; falls back to default then first).
- `server.py` — `POST /api/intake/understand`, `POST /api/intake/commit`;
  `intake_enabled` flag added to `GET /api/health`; live product catalog wired.
- `prototype/fridge-now.html` — **🎙 Add** button (top-right header), mic capture
  via browser `SpeechRecognition`, editable review sheet (name/qty/unit/due
  date/opened, drop rows), commit + snackbar. Add button dims if intake disabled.
- `config.py` / `.env.example` — `FOODBRAIN_OPENROUTER_API_KEY`,
  `FOODBRAIN_OPENROUTER_MODEL`, `FOODBRAIN_OPENROUTER_BASE_URL`,
  `FOODBRAIN_INTAKE_DEFAULT_LOCATION` / `_UNIT`.
- `tests/test_intake.py` — 18 tests.

**Verified live (read-only) from the dev box:** with the dev-box `.env`, Gemini
3.5 Flash via OpenRouter correctly parsed the milk/carrots sentence, matched
"milk" → existing German product **Milch** (via alias map), and flagged carrots
as `new` (correct — the household has no carrot product). Real Grocy products
today: Eier, Milch, Dijon Mustard, Harissa, snackster mini gurken,
Landjoghurt mild 1,5% Fett. **No live writes were performed.**

### Config facts

- **Model slug:** must be `google/gemini-3.5-flash` (user first wrote
  `gemini-3.5-flash` with no provider prefix — that's invalid on OpenRouter;
  fixed in dev `.env`). `google/gemini-3.5-flash` IS a real OpenRouter model.
- **Dev-box `.env`** (`c:\Users\eiwen\foodbrain\.env`) already has
  `FOODBRAIN_OPENROUTER_API_KEY=sk-or-v1-…` and
  `FOODBRAIN_OPENROUTER_MODEL=google/gemini-3.5-flash`.
- **`.env` is gitignored** → the key does NOT travel with `git pull`. It must be
  added on CT 105 by hand.

### REMAINING STEPS (do these next)

**SSH access from the dev box works** (confirmed reachable, user authorized it):
`ssh -i ~/.ssh/id_ed25519 root@192.168.178.100` (Proxmox host `pve`). Drive the
container with `pct exec 105 -- <cmd>` (no container password needed). NOTE: the
last SSH attempt was interrupted by the user mid-run; nothing was changed on the
servers yet. Re-confirm authorization before connecting, or just hand the user
copy-paste commands for their `root@pve` shell.

**1. Deploy code + key to CT 105:**
```
pct exec 105 -- git -C /opt/foodbrain pull            # pulls 66b0a75
pct exec 105 -- bash -c "grep -q OPENROUTER /opt/foodbrain/.env || printf 'FOODBRAIN_OPENROUTER_API_KEY=sk-or-v1-…\nFOODBRAIN_OPENROUTER_MODEL=google/gemini-3.5-flash\n' >> /opt/foodbrain/.env"
pct exec 105 -- systemctl restart foodbrain
pct exec 105 -- curl -s localhost:8123/api/health      # want: "intake_enabled": true
```
(Use the real key from the dev-box `.env`.)

**2. HTTPS so the phone mic works — THIS IS REQUIRED (chosen approach: Tailscale).**
Browsers only allow `SpeechRecognition`/mic on a **secure context** (HTTPS or
localhost). The HA panel is plain HTTP on a LAN IP, so the 🎙 button is **blocked
on the phone** until HTTPS. Typing still works over HTTP; understanding/writing
are server-side and fine. User picked **Tailscale** (trusted Let's Encrypt cert
on a `*.ts.net` name, works home + away).

CT 105 is an **unprivileged LXC** → Tailscale needs `/dev/net/tun`, not exposed
by default. On the **Proxmox host** (`root@pve`):
```
echo -e "lxc.cgroup2.devices.allow: c 10:200 rwm\nlxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file" >> /etc/pve/lxc/105.conf
pct restart 105
```
Inside CT 105 (`pct enter 105`):
```
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up          # prints a login URL — USER must open it in their browser (interactive; agent can't do this step)
tailscale serve --bg 8123
tailscale serve status # shows https://foodbrain.<tailnet>.ts.net  (CT 105 hostname is "foodbrain")
```
First `serve` will prompt to enable HTTPS/MagicDNS in the Tailscale admin console
(one toggle, user clicks).
Fallback if tun won't work: `tailscale up --tun=userspace-networking` (serve still works in userspace mode).

**3. Phone:** install Tailscale app, sign in (same account), toggle ON. Open the
`https://foodbrain.<tailnet>.ts.net/ui` URL (Add to Home Screen). Tap 🎙 Add → talk.
Optionally update the HA "Webpage" dashboard URL to the https one (https-in-http
iframe is allowed).

### Cleanup / follow-ups
- **Stray Tailscale serve on the GROCY box (CT 104):** while fumbling, the user
  ran `tailscale serve --bg 80` on `grocy` (CT 104, .150) — exposed Grocy's web
  UI over the tailnet, wrong machine/port. Clean up with `tailscale serve reset`
  on CT 104 (clears ALL serve config there — confirm no other shares first).
- **Rotate the OpenRouter key:** it appeared in plaintext in the chat several
  times. Recommend regenerating it in the OpenRouter dashboard after setup and
  updating both `.env` files. (Grocy API key also appeared; rotate if desired.)
- **Optional prompt tweak:** model translates unmatched items to English
  ("carrots"→"carrot"), so newly *created* products get English names even though
  the household uses German. If user wants new products to keep the spoken
  language, nudge the prompt in `intake.py` (`_SYSTEM_PROMPT`).
- **Next slice:** photo intake (snap a shelf) reuses the same
  understand→reconcile→commit pipeline with a vision model (gemini-3.5-flash is
  multimodal, so the same key/model works).

### Machine map (quick ref)
| Machine | IP | Role |
|---|---|---|
| `pve` (Proxmox host) | 192.168.178.100 | has `pct`; SSH key-auth works from dev box |
| CT 104 `grocy` | 192.168.178.150 | Grocy (has stray tailscale serve to clean) |
| CT 105 `foodbrain` | 192.168.178.151 | FoodBrain server `/opt/foodbrain`, `foodbrain.service`, `:8123` |
| VM 102 `homeassistant` | homeassistant.local:8123 | HA (plain HTTP); FoodBrain panel = Webpage dashboard |

---

## PROJECT COMPLETE (2026-06-05)

Build order steps 1–4 are **done**, the service is **deployed and live** on the
LAN, reads + writes are verified against real Grocy, and the **FoodBrain panel is
now registered in Home Assistant and loads in the sidebar.** Nothing is pending.

### How the HA panel was actually registered (the YAML way is DEAD)

The original plan was a `panel_iframe:` block in `configuration.yaml`. **That no
longer works** — `panel_iframe` was deprecated and **removed** from Home
Assistant; on HA Core 2026.6.0 the config check fails with
`Integration error: panel_iframe - Integration 'panel_iframe' not found.` Do NOT
use it.

The working replacement is the built-in **"Webpage" (Webseite) dashboard** — no
YAML, no restart:

1. **Settings (Einstellungen) → Dashboards → Add Dashboard (Dashboard
   hinzufügen) → "Webpage" (Webseite).**
2. Fill in: Title `FoodBrain`, Icon `mdi:fridge`, URL
   `http://192.168.178.151:8123/ui`.
3. Save. It appears in the left sidebar immediately and loads the fridge SPA.

HA (VM 102) is plain HTTP (`http://homeassistant.local:8123`), so there's no
mixed-content issue, and the FoodBrain server sets no `X-Frame-Options`/CSP, so
the iframe embed works. **Verified working in the sidebar 2026-06-05.**

If you previously added the `panel_iframe:` block, delete it from
`configuration.yaml` to clear the config warning.

### Deployment (live as of 2026-06-04)

- **Host:** dedicated Debian 12 LXC **CT 105** (`foodbrain`) on Proxmox node `pve`
  (`192.168.178.100`), DHCP IP **192.168.178.151**, unprivileged, onboot=1.
- **Code:** cloned to `/opt/foodbrain` from the **public** repo
  `https://github.com/6f48rhvypg-lang/foodbrain` (runtime is pure stdlib — no pip).
- **Service:** systemd `foodbrain.service` (enabled, `active (running)`),
  `WorkingDirectory=/opt/foodbrain`, serves on `0.0.0.0:8123` with
  `--pairings-json examples/pairings.sample.json --recipes-json examples/recipes.sample.json`.
  `.env` in `/opt/foodbrain/.env` holds the live Grocy URL (`http://192.168.178.150`)
  + API key (matches the dev-box `.env`).
- **Verified from the dev box over the LAN:** `GET /api/health` → ok;
  `GET /api/stock` → live Grocy (Milch, warm, 5d); `GET /ui` → SPA HTML.
- **Grocy** is CT 104 (`192.168.178.150`); **Home Assistant** is VM 102
  (`homeassistant.local:8123`, plain HTTP).
- Manage the service: `pct enter 105` then
  `systemctl status|restart foodbrain`, `journalctl -u foodbrain -f`. To update:
  `cd /opt/foodbrain && git pull && systemctl restart foodbrain`.

### Repo move (2026-06-04)

Canonical repo moved from the work org to the personal account:
`https://github.com/6f48rhvypg-lang/foodbrain` (now **public**). Local `origin`
points there; the old `RSM-CEI/foodbrain` is kept as the `rsm-cei` remote
(backup, not deleted). README + this file updated to the new URL.

### Live verification — reads + writes against real Grocy (2026-06-04)

Done this session against the real Grocy LXC (`.env` configured), so the
standing "writes NOT live-verified" caution from steps 1–4 is now **cleared**:

- **Reads (read-only):** server started against live Grocy; the SPA served at
  `/ui` **same-origin** loaded the real stock (Milch, `warm` band, 5 days) in a
  headless browser with **zero console errors** — the exact HA-panel path.
- **Writes (net-zero):** via the live API endpoints the SPA uses — Milch=2.0 →
  `POST /api/consume` (1) → `transaction_id 6a21bbe9332bd`, `undoable:true` →
  stock=1.0 (real write landed) → `POST /api/undo` → stock back to 2.0. The full
  consume→undo round-trip works against real Grocy with no net inventory change.
- Not exercised live: `toss` and `set-due-date` (toss is the same consume path
  with `spoiled=true`; set-due-date targets a stock entry). Consume+undo proves
  the write/transaction/undo rails; those two are low-risk extensions.

Full blueprint in **[ux-design.md](ux-design.md)** (build order + architecture).

### Step 4 — HA webpage panel (DONE this session)

The FoodBrain server now serves the SPA itself, same-origin with the API, so it
embeds as an HA `panel_iframe` with no CORS in production:

- [server.py](src/foodbrain_assistant/server.py): `make_handler(api, ui_html=None)`
  gained `/`, `/ui`, `/ui/`, `/index.html` → serve the SPA via a new `_send_html`
  (Content-Type `text/html`); when `ui_html` is `None` those routes 404 (pure-API
  server, unchanged behavior). New `_load_ui(args)` auto-detects the in-repo
  `prototype/fridge-now.html` (override with `--ui-file PATH`); `main()` prints the
  `/ui` URL when the SPA is served. The SPA's existing same-origin resolution
  (`API_BASE=''` when served) needed no change.
- README gained a "HA webpage panel (build order step 4)" subsection with the
  `panel_iframe` YAML + the `--host 0.0.0.0` LAN-bind note + the HTTPS caveat.
- Tests: 4 new in [tests/test_api.py](tests/test_api.py) — `UiServingTest` (root +
  `/ui` serve the bytes with the right content-type; API still works alongside the
  UI) and a 404 case when no bundle is loaded. Suite **100 (1 skipped)**.
- VERIFIED in a real headless browser (Playwright): the SPA served at `/ui`
  **same-origin, no `?api=` override** loaded live `--sample` data (4 items across
  all 4 bands, header stats) with **zero console errors**, and a Connect POST
  rendered its result group — confirming the same-origin path the HA panel will use.
  Reads + writes were subsequently **live-verified against real Grocy** too (see
  "Live verification — reads + writes against real Grocy" above).

### Step 3 — SPA (DONE earlier this session)

[prototype/fridge-now.html](prototype/fridge-now.html) is no longer on mock data;
it is wired to the live JSON API:

- Replaced the hardcoded `items` array with `reload()` → `GET /api/stock`. Items
  are mapped from API rows; `band` and `as_of` (→ `TODAY`) come from the API, so
  the SPA grouping always matches the engine. Removed the prototype's local
  `band()` recompute and the dead "recently added" sort chip (the API has no
  added-date field).
- **Connect** → `POST /api/connect`, rendered as pairings grouped per selected
  ingredient (with the `in stock` badge) + the unlocked recipes (coverage bar,
  matched/missing). **Ask AI** → `POST /api/build-prompt` fills the editable
  textarea. Both show a loading state and an in-sheet error fallback.
- Inline quick actions are optimistic with real rollback: **consume**/**toss** →
  `POST /api/consume`|`/api/toss` (toss sends `confirm:true` after the JS
  confirm); **edit due date** → `GET /api/product-entries` to resolve a
  `stock_entry_id`, then `POST /api/set-due-date`. The **undo** snackbar uses the
  `transaction_id` the write returns → `POST /api/undo` then `reload()`. Failures
  revert the optimistic UI and show the error in the snackbar.
- Added `esc()` and escape all Grocy-sourced strings (names/units) before
  `innerHTML`. API base resolves to same-origin when served, `http://127.0.0.1:8123`
  over `file://`, or `?api=` override. An offline state renders if `/api/stock`
  is unreachable.
- VERIFIED end-to-end against a running `--sample` server (Playwright, headless):
  4 items in 4 bands, header stats, multi-select, and Connect all render live
  data (Carrots → ginger/cumin/honey/rice with rice badged in-stock; recipes
  Carrot Rice Pilaf / Pantry Fried Rice) with no console errors. Reads/connect/
  build-prompt are fully exercised. **Writes were NOT live-verified** — against
  `--sample` the consume call reaches the real Grocy LXC and 400s on the sample
  id (path works, surfaces the error). Same caution as steps 1/2: writes are
  mutating; do a deliberate manual check against a real product before relying on
  them. README gained an "SPA (build order step 3)" subsection.

### Step 2 — JSON API (DONE earlier this session)

- New **[api.py](src/foodbrain_assistant/api.py)** = transport-agnostic
  `FoodBrainAPI` (a frozen dataclass) holding settings + a `stock_provider`
  callable + optional `recipes`/`pairings`/`aliases` + a `write_client_factory`.
  Every operation returns a JSON-serializable dict and is unit-testable without a
  socket. Operations:
  - `stock_with_scores()` — bands view; scores every in-stock item and tags its
    band (`hot`≤0d, `warm`≤window, `cool`>window, `staple`=no due date), matching
    the prototype thresholds. Returns `items` (sorted most-urgent-first) +
    `summary` (counts).
  - `connect(selection)` — `selection` is a list of **product ids**; resolves to
    stock items, returns flavor pairings among them (`pairing.suggest_pairings`)
    + the recipes that selection *unlocks* (recipes that call for ≥1 selected
    item, ranked by `matching.rank_recipes` against full stock).
  - `build_prompt(selection)` — editable LLM prompt text; **no LLM call**.
  - `consume` / `toss` / `set_due_date` / `undo` / `product_entries` — proxies
    onto `writeback.py`. `ApiError(status, message)` carries the HTTP status;
    toss-without-confirm → 409, writes-disabled → 403, Grocy failure → 502.
- New **[server.py](src/foodbrain_assistant/server.py)** = thin stdlib
  `http.server` transport (runtime stays dependency-free). Routes + a `--sample` /
  `--stock-json` / live-Grocy bootstrap; permissive CORS for SPA dev. Run:
  `python3 -m foodbrain_assistant.server --sample --pairings-json examples/pairings.sample.json --recipes-json examples/recipes.sample.json`.
- README gained a "JSON API (build order step 2)" subsection with the route table.
- Tests: **[tests/test_api.py](tests/test_api.py)** (22 new) — pure `FoodBrainAPI`
  unit tests + a real-socket HTTP smoke test via `make_handler`. Suite **96 (1 skipped)**.
- NOT yet live-verified for **writes** against the real Grocy LXC (same caution as
  step 1 — writes are mutating). Reads/connect/build-prompt verified end-to-end
  over a running server with sample data.

### Housekeeping — untracked harness artifacts

`git status` shows two untracked paths that are **not** FoodBrain code and were
deliberately left out of the step-1/step-2 commits: `.agents/` and
`skills-lock.json` (Claude Code skill tooling). They are safe to add to
`.gitignore` so they stop showing as untracked; not done yet pending a decision.

### Step 1 — Grocy write-back (DONE earlier this session)

- `GrocyClient` is now **read-only by default**; pass `allow_writes=True` to
  enable writes. Any write on a read-only client raises `GrocyWriteDisabledError`
  (the dry-run/test guard from the design).
- New write primitives on `GrocyClient`: `consume_product(id, amount, spoiled=)`,
  `open_product`, `set_entry_due_date(entry_id, date)`, `undo_transaction(tx_id)`,
  plus read helper `get_product_entries(product_id)` → `list[StockEntry]` (needed
  because due-date edits target a stock *entry*, not the product).
- New `writeback.py` module = the safety rails: `consume()` / `toss()` return a
  `WriteOutcome` with the Grocy `transaction_id`; `undo(client, outcome)` reverses
  it. `toss()` raises `ConfirmationRequired` unless `confirm=True` (confirm on
  destructive). `set_due_date()` too.
- New `StockEntry` model; `extract_transaction_id()` + `parse_stock_entries_response()`
  helpers in `grocy_client.py`.
- Tests: `tests/test_grocy_writeback.py` (12 new). Suite now **74 (1 skipped)**.
- README gained a "Grocy write-back" subsection.
- NOT yet live-verified against the real Grocy LXC (writes are mutating; left for
  a deliberate manual check). Request-building is unit-tested via a mocked
  `urlopen`. The HTTP endpoints used are the standard Grocy stock API
  (`/api/stock/products/{id}/consume|open`, `/api/stock/entry/{id}`,
  `/api/stock/transactions/{tx}/undo`) — confirm these against the household
  Grocy version before wiring the SPA.

### Original UX-design context (still current)

The fridge-overview UX was brainstormed and **agreed** earlier. The prototype
mocks the data + Connect/Ask outputs but implements every interaction; it is the
SPA seed for step 3.

Decisions locked (details + rationale in ux-design.md):

- **UI surface:** FoodBrain serves its **own SPA**, embedded in Home Assistant as
  a webpage panel. HA becomes host + notifier; FoodBrain owns the screen. (Chosen
  over native Lovelace/HACS cards and over a custom JS Lovelace card, because
  multi-select, live re-sort, and an editable prompt box are native web-app
  behavior and miserable as Lovelace YAML.)
- **"Ask AI" mode:** build-and-**copy an editable prompt** (no LLM call yet).
  Zero AI infra; Ollama/cloud stays a future toggle behind the same prompt box.
- **Grocy write-back:** full quick actions — **consume / toss-remove / edit due
  date** inline. The service stops being read-only; writes need a safety rail
  (confirm on destructive, undo on consume).

Agreed build order (see ux-design.md "Build order"):

1. ✅ **Grocy write-back** in the Python service (consume / open / edit-due-date)
   with confirm + undo semantics and a read-only-safe guard for tests. DONE.
2. ✅ **JSON API** off the service: `stock-with-scores`, `connect(selection)`,
   `build-prompt(selection)`, plus write proxies. DONE.
3. ✅ **SPA**: urgency-bands view, multi-select, action bar, editable prompt box. DONE.
4. ✅ **Embed** as an HA webpage panel (server serves the SPA same-origin at
   `/ui`); keep the webhook for notifications only. DONE. ← live wire-up next

Reused as-is (the recommendation brains are done): expiry scoring → bands;
`matching.py` + `pairing.py` (+ 144-entry alias map) → "Connect" mode;
`grocy_client.py` → bands data; `home_assistant.py` webhook → notifications.

The code state below is current and green (HEAD c3dbf90, pushed). No code work is
pending from prior phases.

## Prior direction note (superseded by the agreed design above)

Before this session the plan was only to "step back and design the UX." That
design conversation happened and produced ux-design.md; the section above is the
outcome. The kitchen-workflow context that informed it: locations as physical
places, quantity-unit conversions as the foundation for recipe matching,
scan-in/scan-out via the Grocy Android app, track perishables religiously +
ignore shelf-stable staples, "due soon" window for waste prevention.

## Prior Start-Here (superseded by the direction change above)

The **German alias map was expanded this session** from a 16-entry starter to
**144 validated entries** (`examples/aliases.sample.json`). Categories: dairy &
eggs, produce, meat/fish, staples/bakery/pantry, herbs/spices, drinks. Every
target was validated against the real FlavorGraph ingredient node vocabulary
(`.foodbrain-local/flavorgraph/nodes_191120.csv`, 6653 ingredient nodes) and the
generated pairing bundle keys, so each alias resolves to an actual node. The 7
multiword targets that aren't bare nodes (cream cheese, mozzarella cheese,
parmesan cheese, ground beef, olive oil, tomato paste, orange juice) were
confirmed to be exact bundle keys. VERIFIED end to end on this machine against
the real bundle: a German stock (Hähnchen/Knoblauch/Zwiebel/Olivenöl) resolved
to real FlavorGraph pairings, and the in-stock cross-reference fired (Hähnchen ->
"heads of garlic (in stock)" = Knoblauch). Suite still 62 (1 skipped). Commit
`a0b6e49`.

The alias map machinery (aliases.py, the `normalize_ingredient_name(name,
aliases=None)` chokepoint, CLI auto-load of `examples/aliases.sample.json` +
optional gitignored `.foodbrain-local/aliases.json` override + `--aliases-json
PATH`) was built the prior session and is unchanged.

The next recommended task is open — pick from "Next Implementation Decision".
Strongest candidate now: verify recipe+pairing matching against real Grocy
recipes once some exist (household Grocy still has zero recipes). The alias map
could be grown further (it covers common terms, not the full 6653-node
vocabulary) but diminishing returns vs. real-recipe verification.

First commands on a fresh machine:

```bash
git pull --ff-only
PYTHONPATH=src python3 -m unittest discover -s tests   # expect: Ran 62 tests, OK (skipped=1)
PYTHONPATH=src python3 -m foodbrain_assistant.cli --sample --pairings-json examples/pairings.sample.json
# Optional: rebuild the real bundle (see "Generating a real FlavorGraph bundle" in README)
PYTHONPATH=src python3 scripts/build_flavor_pairings.py --min-score 0.5
PYTHONPATH=src python3 -m foodbrain_assistant.cli --grocy-stock-json .foodbrain-local/stock.json \
  --pairings-json .foodbrain-local/pairings.json --aliases-json examples/aliases.sample.json
```

## Current State

- Repository: `https://github.com/6f48rhvypg-lang/foodbrain`
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

### Expanded German alias map to 144 entries (latest)

- Grew `examples/aliases.sample.json` from 16 to 144 German->English entries.
- Method (reproducible): loaded the 6653 ingredient nodes from the local
  `.foodbrain-local/flavorgraph/nodes_191120.csv` and the keys of the generated
  `.foodbrain-local/pairings.json`, authored a German candidate map by category,
  and validated every target either as an exact bundle key or fully
  token-resolvable (singularized) against the bundle. 0 unresolvable.
- Fixed 7 targets to their real node names: cream cheese, mozzarella cheese,
  parmesan cheese, ground beef, olive oil, tomato paste, orange juice.
- README "Non-English ingredient names" note updated (16-entry -> ~144,
  validated against the real vocabulary).
- No code changed; the alias machinery from the prior session is untouched. The
  prior `tests/test_aliases.py` and German-resolution test in `test_pairing.py`
  still pass. Suite 62 (1 skipped). Commit `a0b6e49`.

### English/German ingredient alias map (prior session)

- Added `foodbrain_assistant/aliases.py`: `load_aliases(payload)` validates a flat
  `{ "source": "target" }` JSON object and normalizes both sides; `merge_aliases`
  layers a local override over the sample. Raises `AliasError` on bad input.
- `normalization.normalize_ingredient_name` now takes an optional `aliases` map.
  Aliases apply after lowercasing/whitespace cleanup but before singularization,
  whole normalized name first then per token (so `Milch` and `Bio Milch` both
  map). `None`/absent = unchanged behavior, so the prior 52 tests stayed green.
- Threaded the map through `matching.rank_recipes`/`match_recipe`/`_tokenize`,
  `pairing.suggest_pairings`/`partners_for`/`_tokenize`, and
  `service.run_once_with_source`. Pairing-bundle keys and partner names are NOT
  aliased (they are already English); only stock/recipe names are.
- CLI: new `--aliases-json PATH`. With no flag it auto-loads
  `examples/aliases.sample.json` when present and layers a gitignored
  `.foodbrain-local/aliases.json` override on top.
- Added `examples/aliases.sample.json` (German starter map), `tests/test_aliases.py`,
  and a German-resolution test in `tests/test_pairing.py`. Suite: 62 (1 skipped).
- README gained a "Non-English ingredient names (alias map)" subsection.

### Real FlavorGraph bundle generator (earlier)

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
Ran 62 tests
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
git clone https://github.com/6f48rhvypg-lang/foodbrain.git
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

Phase 4 recipe matching is implemented and verified live. Phase 5 FlavorGraph
pairing is implemented with a real bundle generator. The **English/German alias
map is now also done and verified** (see "English/German ingredient alias map"
above). All planned data/normalization tasks are therefore complete.

There is no single mandated next task. Pick from the open options below; the
strongest are verifying matching against real Grocy recipes (once any exist) and
expanding the alias map from the real FlavorGraph vocabulary.

To regenerate the bundle, re-download the two FlavorGraph artifacts into
`.foodbrain-local/flavorgraph/` (see README "Generating a real FlavorGraph
bundle") and re-run `scripts/build_flavor_pairings.py`. The generated bundle and
the embeddings stay under `.foodbrain-local/` (gitignored); never commit them.

Open options:

- Grow `examples/aliases.sample.json` (and/or a private
  `.foodbrain-local/aliases.json`) further from the real FlavorGraph node
  vocabulary. The sample now covers 144 common German terms (validated against
  the real nodes); the full vocabulary is 6653 nodes, so rarer products still
  miss. Diminishing returns vs. real-recipe verification.

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