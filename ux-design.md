# FoodBrain UX Design — Fridge Overview & Recipe Integration

Status: design agreed 2026-06-04. No code written yet. This is the blueprint the
next implementation sessions build against. It supersedes "jump straight to
recipe matching" — the fridge-overview UX comes first, recipe/AI features hang
off it.

## North star

Open the Companion app, see **what's in the fridge right now**, instantly know
**what needs eating next**, fix wrong data in one tap, and — when the fridge
feels empty — get help turning odds-and-ends into something to cook. FoodBrain
owns the screen; Home Assistant hosts it and sends notifications.

## Decisions locked (2026-06-04)

| Question | Decision | Consequence |
| --- | --- | --- |
| Where the UI lives | **FoodBrain serves its own SPA**, embedded in HA as a webpage panel | Full design freedom; multi-select + editable prompt are natural; reusable outside HA. The service gets a `/ui` route + a JSON API. |
| What "Ask AI" does | **Build-and-copy an editable prompt** (no LLM call yet) | Zero AI infra. Ollama/cloud stays a future toggle behind the same prompt box (roadmap Phase 8). |
| Grocy write-back | **Full quick actions: consume, toss/remove, edit due date** | Service stops being read-only. Writes need a safety rail (confirm on destructive, undo on consume). |

### Why an embedded web app over native HA cards

Three tiers of HA "design freedom" were considered:
1. Native Lovelace + HACS cards (`button-card`, `auto-entities`, `flex-table-card`)
   — fastest, but tap-to-multi-select, live re-sort, and an *editable* prompt box
   are painful-to-impossible in YAML templating.
2. Custom Lovelace card written in JS — full HTML/CSS, but tied to HA's card
   lifecycle and data plumbing.
3. **Own SPA embedded as a webpage/iframe panel** — 100% design freedom, shows up
   in the Companion app like a native screen, reusable in a plain browser.

Chose (3): FoodBrain is already a Python service, so adding a small frontend +
JSON API is a modest step, and the interactions we want are native web-app
behavior, not Lovelace behavior.

## Visual direction & prototype

A working, self-contained high-fidelity prototype lives at
**[prototype/fridge-now.html](prototype/fridge-now.html)** — open it directly in a
browser (no build step, no server). It is the reference for look + interactions
and is the seed for the eventual SPA (step 3 of the build order). Built with the
`frontend-design` skill.

**Aesthetic: "inside the fridge at night."** Cold frosted-glass dark UI; each
urgency band is a lit shelf. Rationale: opening the app should feel like opening
the fridge, and a dark cold field makes produce-colored urgency badges pop for
the at-a-glance "what's about to die?" read.

- **Type:** Fraunces (characterful serif) for item/section names · IBM Plex Mono
  for the "appliance readout" data (quantities, due-in-Nd, labels) · Hanken
  Grotesk for UI/body. Deliberately not Inter/Roboto/Space Grotesk.
- **Color = temperature scale:** hot red (`--hot`, eat today/overdue) → amber
  (`--warm`, this week) → mint (`--cool`, fresh) → muted (`--staple`). Each band
  carries the color on a "temperature" bar and the item's left spine + due pill.
- **Motion:** staggered card rise on load; overdue badges pulse-glow; action bar
  slides up on selection; sheet slides up; consume animates a collapse-out with an
  undo snackbar.

The prototype implements every interaction in this doc against mock German stock
(matching the real Grocy + alias-map setup): bands, sort chips, collapsible
staples, multi-select → action bar, Connect sheet (mock pairings + recipes), Ask
sheet (editable prompt + copy), inline edit-date popover (quick chips + picker),
consume/toss with confirm + undo. The data and the Connect/Ask outputs are
mocked; wiring them to the real `scoring`/`matching`/`pairing` + a Grocy
write-back API is the implementation work (build order steps 1–3).

## Screens

### 1. Fridge now (default) — urgency bands

Not a flat date-sorted list. Group into glanceable bands so the empty-fridge
"what's about to die?" question is answered at a glance:

```
🔴 Eat today / overdue   [ Joghurt   ·1·  in -1d ]  ✓ 🗑 ✎
🟠 This week             [ Zucchini  ·2·  in 3d  ]  ✓ 🗑 ✎
                         [ Feta      ·1·  in 5d  ]  ✓ 🗑 ✎
🟢 Fresh                 [ …                     ]
⚪ Staples (collapsed)   ▸ 12 items
```

- Bands derive from the existing expiry-urgency scoring (`scoring.py`). No new
  scoring model needed for v1.
- **Staples collapse by default** — matches the household principle "track
  perishables religiously, ignore shelf-stable staples." A staple = shelf-stable
  / no practical due date (Grocy `2999-12-31` already treated as no expiry).
- Each row shows: name · quantity · "in N days" badge.
- **Inline quick actions per row:** `✓ used` / `🗑 toss` / `✎ edit date`. On
  mobile, swipe-to-consume. No separate edit screen.
- **Sort control** re-orders (by due date, name, quantity, location, recently
  added). Sorting can flatten the bands into one ranked list when chosen.

OPEN: inline "edit date" interaction not yet pinned down (date picker popover vs.
quick +1d/+3d/+1w chips). Lean toward quick chips + a "pick date" fallback.

OPEN: should locations (fridge / pantry / freezer) be top-level tabs, or a
filter/group-by within one list? Leaning filter-first, tabs later if needed.

### 2. Selection → action bar

Tap items to multi-select. An action bar slides up with two modes:

- **Connect (deterministic, FoodBrain):**
  - Flavor pairings among the selected items (`pairing.py`).
  - Which recipes the selection unlocks, ranked by expiry usefulness
    (`matching.py`).
  - Fully explainable, no AI.
- **Ask (generative):**
  - Opens an **editable prompt** prefilled from the selection, e.g.
    *"I have leftover zucchini, ½ onion, feta. Suggest 3 simple dinners for
    tonight using mostly these plus common staples."*
  - User edits → **Copy** to clipboard. (Later: a "Send to Ollama" toggle behind
    the same box.)

### 3. Empty-fridge hero case

When the fresh bands are thin, do NOT show an empty list. Proactively surface
"odds & ends" pantry combos (Connect-mode suggestions over leftovers + pantry).
This is the emotional moment the whole app is designed around.

## Architecture impact

```
Grocy  <--read/write-->  FoodBrain service  --serves-->  SPA (webpage panel in HA)
                              |
                              +--webhook--> Home Assistant (notifications only)
```

- **New: Grocy write-back** (the real new backend muscle):
  - consume: `POST /api/stock/products/{id}/consume`
  - open: `POST /api/stock/products/{id}/open`
  - edit due date: edit the stock entry (`PUT /api/stock/entry/{entryId}` /
    `/api/objects/stock/{id}` with `best_before_date`)
  - **Safety rail:** confirm on toss/remove; undo on consume. Cheap up front,
    annoying to retrofit.
- **New: JSON API** off the existing service:
  - `GET stock-with-scores` — bands view data (items + urgency + band).
  - `POST connect(selection)` — pairings + unlocked recipes for selected items.
  - `POST build-prompt(selection)` — the editable prompt text.
  - write endpoints proxying the Grocy actions above.
- **New: SPA** — bands view, multi-select, action bar, prompt box. Served at
  `/ui`.
- **Reuse: webhook** stays, but for notifications only ("3 things expire
  tomorrow"), not as the primary surface.
- Keep the runtime dependency-free where reasonable (current design principle);
  the SPA can be a single static bundle served by the Python service.

## Build order

1. ✅ **Grocy write-back** in the service — consume / open / edit-due-date, with
   confirm + undo semantics. Read-only-safe: `GrocyClient(allow_writes=False)` is
   the default and refuses writes. DONE 2026-06-04 (`grocy_client.py` write
   primitives + `writeback.py` rails + `tests/test_grocy_writeback.py`).
2. ✅ **JSON API** — `stock-with-scores`, `connect`, `build-prompt`, write
   proxies. DONE 2026-06-04 (`api.py` transport-agnostic `FoodBrainAPI` +
   `server.py` stdlib `http.server` transport + `tests/test_api.py`).
3. **SPA** — bands, multi-select, action bar, prompt box. ← start here
4. **Embed** as an HA webpage panel; keep the webhook for notifications.

## What already exists and is reused

- Expiry-urgency scoring → the bands. (`scoring.py`)
- Recipe matching ranked by coverage + expiry usefulness → Connect mode.
  (`matching.py`)
- FlavorGraph pairing (real bundle generator + 144-entry German alias map) →
  Connect mode. (`pairing.py`, `aliases.py`)
- Grocy stock read + parser/diagnostics → feeds the bands view. (`grocy_client.py`)
- HA webhook publisher → notifications. (`home_assistant.py`)

The genuinely new work is: write-back, a JSON API, and the frontend. The
recommendation brains are done.

## Open questions to resolve during build

- Inline "edit date" interaction: quick +1d/+3d/+1w chips vs. full date picker.
- Locations as tabs vs. filter/group-by.
- Where the "staple vs perishable" line is drawn for the collapse rule (purely
  no-due-date, or also a product flag in Grocy?).
- Undo window/UX for consume (snackbar with undo vs. a recent-actions list).
