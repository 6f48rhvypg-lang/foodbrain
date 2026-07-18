"""Transport-agnostic JSON API for the FoodBrain SPA (build order step 2).

This is the brain-to-screen layer: it turns the existing recommendation engine
(:mod:`scoring`, :mod:`matching`, :mod:`pairing`) and the write-back rails
(:mod:`writeback`) into small, JSON-serializable operations the frontend can
call. It deliberately knows nothing about HTTP — :mod:`foodbrain_assistant.server`
wraps it in an ``http.server`` handler — so every operation here is directly
unit-testable without a socket.

Operations:

* :meth:`FoodBrainAPI.stock_with_scores` — the bands view: every stock item with
  its expiry urgency and its urgency *band* (``hot``/``warm``/``cool``/``staple``),
  matching the prototype's grouping.
* :meth:`FoodBrainAPI.connect` — for a multi-select of stock items, the flavor
  pairings among them plus the recipes that selection unlocks (deterministic,
  no AI).
* :meth:`FoodBrainAPI.build_prompt` — an editable LLM prompt prefilled from the
  selection (no LLM call; the SPA copies the text).
* :meth:`FoodBrainAPI.consume` / :meth:`toss` / :meth:`set_due_date` /
  :meth:`undo` — write proxies onto :mod:`writeback`, with the confirm-on-toss
  and undo-on-consume rails preserved.

Reads use a ``stock_provider`` callable so the same API serves live Grocy, an
exported JSON file, or sample data. Writes need a writable
:class:`~foodbrain_assistant.grocy_client.GrocyClient`; without one configured the
write operations fail closed with a 403-style :class:`ApiError`.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import cookmemory, recipes_llm, shopping_llm, shoppingstore
from .grocy_client import (
    GrocyClient,
    GrocyClientError,
    GrocyWriteDisabledError,
    extract_transaction_id,
)
from .llm import LlmError, LlmNotConfigured
from .intake import (
    IntakeError,
    IntakeItem,
    IntakeNotConfigured,
    IntakeResult,
    reconcile_items,
    understand_transcript,
)
from .matching import rank_recipes
from .models import IngredientUrgency, Recipe, StockItem
from .normalization import (
    normalize_ingredient_name,
    tokenize as _recipe_tokenize,
    tokens_match as _tokens_match,
)
from .pairing import PairingGraph, suggest_pairings
from .scoring import score_stock_item
from .writeback import (
    ConfirmationRequired,
    WriteOutcome,
    _live_stock_amount,
    consume,
    rename_product,
    set_amount,
    set_due_date,
    set_location,
    toss,
    undo,
)


class ApiError(RuntimeError):
    """An API-level error carrying the HTTP status the transport should use."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# Band thresholds mirror prototype/fridge-now.html so the SPA grouping matches.
def band_for(days_until_expiry: Optional[int], expiry_window_days: int) -> str:
    if days_until_expiry is None:
        return "staple"
    if days_until_expiry <= 0:
        return "hot"
    if days_until_expiry <= expiry_window_days:
        return "warm"
    return "cool"


BAND_ORDER = ["hot", "warm", "cool", "staple"]


@dataclass(frozen=True)
class FoodBrainAPI:
    settings: object  # foodbrain_assistant.config.Settings
    stock_provider: Callable[[], List[StockItem]]
    recipes: Optional[List[Recipe]] = None
    pairings: Optional[PairingGraph] = None
    aliases: Optional[Dict[str, str]] = None
    write_client_factory: Optional[Callable[[], GrocyClient]] = None
    today_provider: Callable[[], date] = date.today
    source: str = "grocy"
    # Voice intake (talk-at-the-fridge). The catalog provider returns the
    # product master list ([{"id","name"}]); the understander is injectable so
    # tests can stub the model call. Both default to None and degrade safely.
    product_catalog_provider: Optional[Callable[[], List[dict]]] = None
    intake_understander: Optional[Callable[..., IntakeResult]] = None
    # Recipe inspiration. The three generators are injectable (tests stub the
    # model call); when None the real recipes_llm.* functions run. cook_store_path
    # locates the durable learning store (cookmemory.json); when None it's derived
    # from settings.data_dir.
    idea_generator: Optional[Callable[..., dict]] = None
    recipe_generator: Optional[Callable[..., dict]] = None
    recipe_reviser: Optional[Callable[..., dict]] = None
    twist_extractor: Optional[Callable[..., dict]] = None
    consumption_estimator: Optional[Callable[..., dict]] = None
    chat_generator: Optional[Callable[..., dict]] = None
    cook_store_path: Optional[object] = None  # str | Path
    # Shopping list diet-focus suggestions (injectable; None runs the real
    # shopping_llm.suggest_diet_items). shopping_store_path locates the durable
    # overlay/habits store (shopping.json); when None it's derived from
    # settings.data_dir, mirroring cook_store_path.
    diet_suggester: Optional[Callable[..., dict]] = None
    shopping_store_path: Optional[object] = None  # str | Path
    # Best-effort MHD (best-before) estimate for shopping-list purchases, which —
    # unlike voice intake — never asked the model for a shelf life. Injectable
    # for tests; None runs the real shopping_llm.estimate_shelf_life, and any
    # failure/missing config just books items without a date (unchanged from
    # before this existed).
    shelf_life_estimator: Optional[Callable[..., Dict[str, int]]] = None

    # --- reads -----------------------------------------------------------

    def stock_with_scores(self) -> dict:
        """The bands view: every item scored and tagged with its urgency band."""
        today = self.today_provider()
        window = self.settings.expiry_window_days
        items = self.stock_provider()

        scored = [
            _serialize_scored_item(
                score_stock_item(item, today=today, expiry_window_days=window), window
            )
            for item in items
            if item.amount > 0
        ]
        # Most urgent first, then name, so the SPA can render top-down.
        scored.sort(key=lambda row: (-row["urgency_score"], row["name"].lower()))

        counts = {band: 0 for band in BAND_ORDER}
        for row in scored:
            counts[row["band"]] += 1

        return {
            "source": self.source,
            "as_of": today.isoformat(),
            "expiry_window_days": window,
            "band_order": BAND_ORDER,
            "summary": {
                "total": len(scored),
                "urgent": counts["hot"] + counts["warm"],
                "band_counts": counts,
            },
            "items": scored,
        }

    def connect(self, selection: List[str]) -> dict:
        """Pairings among the selection + the recipes it unlocks."""
        today = self.today_provider()
        window = self.settings.expiry_window_days
        stock = self.stock_provider()
        selected = _resolve_selection(stock, selection)
        if not selected:
            raise ApiError(400, "selection did not match any in-stock products")

        selected_urgencies = sorted(
            (
                score_stock_item(item, today=today, expiry_window_days=window)
                for item in selected
            ),
            key=lambda u: (-u.urgency_score, u.item.name.lower()),
        )

        pairings = self._connect_pairings(selected_urgencies, stock)
        recipes = self._connect_recipes(selected, stock, today, window)

        return {
            "selection": [item.name for item in selected],
            "pairings": pairings,
            "recipes": recipes,
            "pairings_loaded": self.pairings is not None,
            "recipes_loaded": bool(self.recipes),
        }

    def build_prompt(
        self, selection: List[str], preferences: Optional[dict] = None
    ) -> dict:
        """An editable German LLM prompt prefilled from the selection + mood.

        ``preferences`` carries the "food mood" answers the SPA collects:
        ``cuisine`` (one of :data:`_CUISINES`), ``style`` (one of
        :data:`_STYLES`), and ``needs`` (a list drawn from :data:`_NEEDS`).
        All are optional; the prompt degrades gracefully when none are given.
        No LLM is called — the SPA copies the returned text.
        """
        stock = self.stock_provider()
        selected = _resolve_selection(stock, selection)
        if not selected:
            raise ApiError(400, "selection did not match any in-stock products")

        listed = ", ".join(_describe_item(item) for item in selected)
        prompt = _german_prompt(listed, preferences or {})
        return {
            "selection": [item.name for item in selected],
            "prompt": prompt,
        }

    def product_entries(self, product_id: str) -> dict:
        """List a product's individual stock entries (for the edit-date flow)."""
        client = self._reader()
        try:
            entries = client.get_product_entries(product_id)
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc
        return {
            "product_id": product_id,
            "entries": [
                {
                    "stock_entry_id": entry.stock_entry_id,
                    "amount": entry.amount,
                    "best_before_date": entry.best_before_date.isoformat()
                    if entry.best_before_date
                    else None,
                    "opened": entry.opened,
                }
                for entry in entries
            ],
        }

    # --- writes (proxies onto writeback.py rails) ------------------------

    def consume(self, product_id: str, amount: float = 1.0) -> dict:
        return self._write(
            lambda client: consume(client, product_id, amount),
            after=self._maybe_record_depletion,
        )

    def toss(self, product_id: str, amount: float = 1.0, *, confirm: bool = False) -> dict:
        return self._write(
            lambda client: toss(client, product_id, amount, confirm=confirm),
            after=self._maybe_record_depletion,
        )

    def set_due_date(
        self, stock_entry_id: str, best_before_date: str, *, product_id: str = ""
    ) -> dict:
        parsed = _parse_iso_date(best_before_date)
        return self._write(
            lambda client: set_due_date(
                client, stock_entry_id, parsed, product_id=product_id
            )
        )

    def get_locations(self) -> dict:
        """List all Grocy storage locations for the location-picker UI."""
        client = self._reader()
        try:
            locs = client.get_locations()
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc
        return {"locations": locs}

    def set_location(
        self, stock_entry_id: str, location_id: str, *, product_id: str = ""
    ) -> dict:
        """Move a stock entry to a different location."""
        return self._write(
            lambda client: set_location(
                client, stock_entry_id, location_id, product_id=product_id
            )
        )

    def set_name(self, product_id: str, name: str) -> dict:
        """Rename a product in the Grocy master catalogue."""
        if not name.strip():
            raise ApiError(400, "name must not be empty")
        return self._write(lambda client: rename_product(client, product_id, name.strip()))

    def set_amount(self, product_id: str, new_amount: float) -> dict:
        """Correct the stock amount for a product via inventory."""
        if new_amount < 0:
            raise ApiError(400, "new_amount must be >= 0")
        return self._write(lambda client: set_amount(client, product_id, new_amount))

    def undo(self, transaction_id: str) -> dict:
        if not transaction_id:
            raise ApiError(400, "transaction_id is required to undo")
        client = self._writer()
        try:
            undo(client, transaction_id)
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc
        return {"action": "undo", "transaction_id": transaction_id, "ok": True}

    # --- shopping list -----------------------------------------------------
    #
    # Grocy's shopping_list object is the source of truth for the items
    # themselves (shared, survives, other Grocy clients stay in sync);
    # shoppingstore.py is a thin metadata overlay (why an item is on the
    # list) plus the learned buying habits that drive the suggestion feed.

    def shopping_products(self) -> dict:
        """The tracked-product catalog (id + name only) for the add-item typeahead —
        picking a suggestion instead of free-typing is what stops a typo or a
        household-specific synonym (``Kuhmilch`` vs ``Milch``) from becoming its
        own orphan product."""
        return {"products": [{"id": str(p["id"]), "name": p["name"]} for p in self._catalog()]}

    def shopping_list(self) -> dict:
        """The shared list (Grocy items + overlay reasons) plus a reasoned
        suggestion feed, and ``rev`` so a poller only re-renders on change.

        ``mode:"auto"`` staples that aren't on the list yet are added to
        Grocy right here (best-effort, deduped against what's already there)
        — this is the only place auto-add happens, so it fires whenever a
        device has the list open and polls.
        """
        client = self._reader()
        try:
            rows = client.get_shopping_list()
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc

        catalog = {str(p["id"]): p["name"] for p in self._catalog()}
        existing_names = {self._shopping_item_name(row, catalog).strip().lower() for row in rows}
        suggestions = self._shopping_suggestions(existing_names)

        added_any = False
        if self.write_client_factory is not None:
            for suggestion in suggestions:
                if suggestion.get("mode") != "auto":
                    continue
                if self._shopping_try_add(
                    client,
                    name=suggestion["name"],
                    product_id=suggestion.get("product_id"),
                    amount=suggestion.get("suggested_amount") or 1.0,
                    reason=suggestion["reason"],
                    source=suggestion["signal"],
                    existing_rows=rows,
                ):
                    added_any = True
            if added_any:
                try:
                    rows = client.get_shopping_list()
                except GrocyClientError as exc:
                    raise ApiError(502, str(exc)) from exc
                added_names = {self._shopping_item_name(r, catalog).strip().lower() for r in rows}
                suggestions = [s for s in suggestions if s["name"].strip().lower() not in added_names]

        path = self._shopping_path()
        valid_ids = [str(row.get("id")) for row in rows if row.get("id") is not None]
        overlay = shoppingstore.prune_overlay(path, valid_ids)
        items = [self._shopping_row(row, overlay, catalog) for row in rows]
        return {
            "items": items,
            "suggestions": suggestions,
            "rev": _shopping_rev(rows, overlay),
        }

    def shopping_add(
        self,
        *,
        name: str = "",
        product_id: Optional[str] = None,
        amount: float = 1.0,
        unit: Optional[str] = None,
        source: str = "manual",
        reason: str = "",
    ) -> dict:
        """Add an item to the shared list — a tracked product or free text."""
        name = str(name or "").strip()
        product_id = str(product_id).strip() if product_id else None
        if not name and not product_id:
            raise ApiError(400, "name or product_id is required")
        client = self._writer()
        if not product_id and name:
            product_id = _ProductIndex(client.get_products(), self.aliases).resolve(name) or None
        qu_id = None
        if unit:
            qu_id = _NameResolver(client.get_quantity_units(), None).resolve(unit) or None
        try:
            item_id = client.add_shopping_item(
                product_id=product_id,
                note=None if product_id else (name or None),
                amount=amount,
                qu_id=qu_id,
            )
        except GrocyWriteDisabledError as exc:
            raise ApiError(403, str(exc)) from exc
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc
        entry = shoppingstore.set_overlay(self._shopping_path(), item_id, source=source, reason=reason)
        return {
            "id": item_id,
            "product_id": product_id,
            "name": name,
            "amount": amount,
            "ok": True,
            **entry,
        }

    def shopping_update(
        self,
        item_id: str,
        *,
        done: Optional[bool] = None,
        amount: Optional[float] = None,
        name: Optional[str] = None,
        product_id: Optional[str] = None,
    ) -> dict:
        """Toggle done / correct the amount / fix a row's name or product match.

        ``product_id`` re-links the row to a specific tracked product (typeahead
        pick — clears any free-text note). ``name`` alone rewrites the free-text
        note (typo fix) and un-links any product match — the two are mutually
        exclusive edit intents from the client.
        """
        item_id = str(item_id or "").strip()
        if not item_id:
            raise ApiError(400, "item_id is required")
        changes: dict = {}
        if done is not None:
            changes["done"] = "1" if done else "0"
        if amount is not None:
            changes["amount"] = amount
        if product_id:
            changes["product_id"] = str(product_id)
            changes["note"] = None
        elif name is not None:
            name = name.strip()
            if not name:
                raise ApiError(400, "name cannot be empty")
            changes["note"] = name
            changes["product_id"] = None
        if not changes:
            raise ApiError(400, "nothing to update")
        client = self._writer()
        try:
            client.update_shopping_item(item_id, changes)
        except GrocyWriteDisabledError as exc:
            raise ApiError(403, str(exc)) from exc
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc
        return {"id": item_id, "ok": True, **changes}

    def shopping_remove(self, item_id: str) -> dict:
        item_id = str(item_id or "").strip()
        if not item_id:
            raise ApiError(400, "item_id is required")
        client = self._writer()
        try:
            client.remove_shopping_item(item_id)
        except GrocyWriteDisabledError as exc:
            raise ApiError(403, str(exc)) from exc
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc
        shoppingstore.remove_overlay(self._shopping_path(), item_id)
        return {"id": item_id, "ok": True}

    def shopping_staple(
        self, name: str, *, product_id: Optional[str] = None, mode: Optional[str] = None
    ) -> dict:
        """Set the user's suggestion control for a product: auto/suggest/off/None."""
        name = str(name or "").strip()
        if not name:
            raise ApiError(400, "name is required")
        try:
            habit = shoppingstore.set_mode(
                self._shopping_path(), name=name, product_id=product_id or "", mode=mode
            )
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
        return {"name": name, "mode": habit["mode"], "ok": True}

    def shopping_staples(self) -> dict:
        """Every learned habit, staple or not, for the "Vorräte verwalten" settings
        screen — unlike :meth:`shopping_list`'s suggestion feed, this is unfiltered:
        it includes staples with no current restock need and ones the user has
        turned ``off``, since the user needs to see and re-pin those too."""
        catalog = {str(p["id"]): p["name"] for p in self._catalog()}
        habits = shoppingstore.habits(self._shopping_path())
        staples = []
        for key, habit in habits.items():
            stats = shoppingstore.habit_stats(habit)
            product_id = habit.get("product_id") or None
            name = catalog.get(str(product_id), "") if product_id else ""
            staples.append(
                {
                    "name": name or key,
                    "product_id": product_id,
                    "mode": habit.get("mode"),
                    "buy_count": len(habit.get("buys") or []),
                    "typical_amount": stats["typical_amount"],
                    "median_interval_days": stats["median_interval_days"],
                    "is_staple": stats["is_staple"],
                }
            )
        staples.sort(key=lambda s: (-s["buy_count"], s["name"]))
        return {"staples": staples}

    def shopping_get_diet_focus(self) -> dict:
        """The persisted diet-focus setting — sticky until the user changes it."""
        return shoppingstore.get_diet_focus(self._shopping_path())

    def shopping_set_diet_focus(self, *, chips: Optional[List[str]] = None, freetext: str = "") -> dict:
        return shoppingstore.set_diet_focus(self._shopping_path(), chips=chips, freetext=freetext)

    def shopping_commit_bought(self, items: List[dict]) -> dict:
        """Book checked-off items into Grocy stock and remove them from the list.

        Reuses the intake add path (create-or-reuse product, add pack) and
        records a ``buy`` event per item so the suggestion engine learns the
        household's rhythm. Per-item resilience mirrors ``intake_commit``.

        Also estimates a best-before date per item up front (one call for the
        whole batch) — the shopping list has no MHD input, unlike voice intake.
        """
        if not items:
            raise ApiError(400, "no items to commit")
        client = self._writer()
        resolvers = (
            _NameResolver(client.get_quantity_units(), self.settings.intake_default_unit),
            _NameResolver(client.get_locations(), self.settings.intake_default_location),
        )
        product_index = _ProductIndex(client.get_products(), self.aliases)
        freshness = self._estimate_shelf_life(items)
        added: List[dict] = []
        failed: List[dict] = []
        for raw in items:
            name = str(raw.get("name") or "").strip()
            try:
                added.append(
                    self._shopping_commit_one(client, raw, resolvers, product_index, freshness)
                )
            except GrocyWriteDisabledError as exc:
                raise ApiError(403, str(exc)) from exc
            except (GrocyClientError, ApiError) as exc:
                message = exc.message if isinstance(exc, ApiError) else str(exc)
                failed.append({"name": name or "?", "error": message})
        return {"added": added, "failed": failed, "ok": not failed}

    def _estimate_shelf_life(self, items: List[dict]) -> Dict[str, int]:
        """Best-effort {lowercased name: days-until-best-before} for a purchase
        batch; empty when the LLM isn't configured or the call fails, so booking
        falls back to no date exactly as before this existed."""
        estimator = self.shelf_life_estimator
        if estimator is None:
            if not self.settings.intake_enabled:
                return {}
            estimator = shopping_llm.estimate_shelf_life
        try:
            return estimator(items, model=self.settings.openrouter_model, settings=self.settings) or {}
        except (LlmError, LlmNotConfigured):
            return {}

    def shopping_diet(self, focus: str) -> dict:
        """LLM-suggested items serving a diet focus (mehr Gemüse, proteinreich, ...).

        On-demand only — never called automatically. Every suggestion carries
        a plain-language ``reason`` (enforced by :mod:`shopping_llm`); the
        client adds whichever it wants via :meth:`shopping_add`.
        """
        self._require_intake()
        focus = str(focus or "").strip()
        if not focus:
            raise ApiError(400, "focus is required")
        model = self._resolve_model(None, self.settings.recipe_model)
        suggester = self.diet_suggester or shopping_llm.suggest_diet_items
        try:
            return suggester(
                focus=focus,
                inventory_lines=self._inventory_lines(),
                taste=cookmemory.taste_summary(self._cook_path()),
                model=model,
                settings=self.settings,
            )
        except (LlmError, LlmNotConfigured) as exc:
            raise self._llm_error(exc) from exc

    def _shopping_commit_one(
        self, client, raw: dict, resolvers, product_index, freshness: Dict[str, int]
    ) -> dict:
        units, locations = resolvers
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ApiError(400, "a bought item needs a name")
        amount = _safe_float(raw.get("amount"), 1.0)
        location_id = locations.resolve(raw.get("location"))
        product_id = _pid(raw)
        if not product_id:
            existing = product_index.resolve(name)
            if existing:
                product_id = existing
            else:
                product_id = client.create_product(
                    name, qu_id_stock=units.resolve(raw.get("unit")), location_id=location_id
                )
                product_index.add(name, product_id)
        days = freshness.get(name.lower())
        best_before = self.today_provider() + timedelta(days=days) if days else None
        client.add_stock(product_id, amount, best_before_date=best_before, location_id=location_id)
        shoppingstore.record_buy(
            self._shopping_path(), name=name, product_id=product_id, amount=amount
        )
        item_id = str(raw.get("item_id") or "").strip()
        if item_id:
            try:
                client.remove_shopping_item(item_id)
            except GrocyClientError:
                pass
            shoppingstore.remove_overlay(self._shopping_path(), item_id)
        return {
            "name": name,
            "product_id": product_id,
            "amount": amount,
            "best_before_date": best_before.isoformat() if best_before else None,
            "ok": True,
        }

    _SHOPPING_SIGNAL_RANK = {"depleted": 0, "low_qty": 1, "interval": 2}

    def _shopping_suggestions(self, existing_names: set) -> List[dict]:
        """Per learned-habit staple not already on the list: a ranked, reasoned row."""
        habits = shoppingstore.habits(self._shopping_path())
        if not habits:
            return []
        stock = self.stock_provider()
        by_pid = {item.product_id: item for item in stock}
        by_name = {item.name.strip().lower(): item for item in stock if item.name}
        now = datetime.now(timezone.utc).timestamp()

        suggestions: List[dict] = []
        for name_key, habit in habits.items():
            if name_key in existing_names:
                continue
            mode = habit.get("mode")
            if mode == "off":
                continue
            stats = shoppingstore.habit_stats(habit)
            if not (stats["is_staple"] or mode in ("auto", "suggest")):
                continue
            item = by_pid.get(habit.get("product_id") or "") or by_name.get(name_key)
            current_amount = item.amount if item else 0.0
            unit = item.unit if item else None
            signal, reason = _shopping_signal(habit, stats, current_amount, unit, now)
            if signal is None:
                continue
            suggestions.append(
                {
                    "name": item.name if item else name_key.title(),
                    "product_id": item.product_id if item else (habit.get("product_id") or None),
                    "suggested_amount": stats["typical_amount"] or 1.0,
                    "unit": unit,
                    "signal": signal,
                    "reason": reason,
                    "current_amount": current_amount,
                    "typical_amount": stats["typical_amount"],
                    "mode": mode,
                }
            )
        suggestions.sort(
            key=lambda s: (self._SHOPPING_SIGNAL_RANK.get(s["signal"], 9), s["name"].lower())
        )
        return suggestions

    def _shopping_try_add(
        self,
        client,
        *,
        name: str,
        product_id: Optional[str],
        amount: float,
        reason: str,
        source: str,
        existing_rows: Optional[List[dict]] = None,
    ) -> bool:
        """Best-effort add to Grocy's list + overlay, skipped if already present."""
        try:
            rows = existing_rows if existing_rows is not None else client.get_shopping_list()
        except GrocyClientError:
            return False
        pid = str(product_id or "")
        key = name.strip().lower()
        for row in rows:
            if pid and str(row.get("product_id") or "") == pid:
                return False
            if not pid and str(row.get("note") or "").strip().lower() == key:
                return False
        try:
            item_id = client.add_shopping_item(
                product_id=product_id or None, note=None if product_id else name, amount=amount
            )
        except GrocyClientError:
            return False
        shoppingstore.set_overlay(self._shopping_path(), item_id, source=source, reason=reason)
        return True

    def _shopping_item_name(self, row: dict, catalog: dict) -> str:
        product_id = str(row.get("product_id") or "")
        return catalog.get(product_id) or str(row.get("note") or "")

    def _shopping_row(self, row: dict, overlay: dict, catalog: dict) -> dict:
        item_id = str(row.get("id") or "")
        product_id = str(row.get("product_id") or "") or None
        ov = overlay.get(item_id, {})
        return {
            "id": item_id,
            "product_id": product_id,
            "name": self._shopping_item_name(row, catalog) or "?",
            "amount": _safe_float(row.get("amount"), 1.0),
            "qu_id": str(row.get("qu_id") or "") or None,
            "done": _truthy(row.get("done")),
            "source": ov.get("source", "manual"),
            "reason": ov.get("reason", ""),
            "added_ts": ov.get("added_ts"),
        }

    def _maybe_record_depletion(self, client, outcome: WriteOutcome) -> None:
        """After a consume/toss lands, log a removal-to-zero for the habit
        it belongs to, and auto-add it back to the list if it's an ``auto`` staple."""
        if outcome.amount <= 0 or not outcome.product_id:
            return
        try:
            if _live_stock_amount(client, outcome.product_id) > 0:
                return
        except GrocyClientError:
            return
        name = self._product_name(outcome.product_id)
        if not name:
            return
        habit = shoppingstore.record_removal(
            self._shopping_path(), name=name, product_id=outcome.product_id
        )
        stats = shoppingstore.habit_stats(habit)
        if habit.get("mode") == "auto" and stats["is_staple"]:
            self._shopping_try_add(
                client,
                name=name,
                product_id=outcome.product_id,
                amount=stats["typical_amount"] or 1.0,
                reason="aufgebraucht",
                source="depleted",
            )

    def _product_name(self, product_id: str) -> str:
        for product in self._catalog():
            if str(product.get("id")) == str(product_id):
                return str(product.get("name") or "")
        return ""

    def _shopping_path(self):
        if self.shopping_store_path is not None:
            return self.shopping_store_path
        data_dir = getattr(self.settings, "data_dir", "data") or "data"
        return Path(data_dir) / "shopping.json"

    # --- voice intake ----------------------------------------------------

    def intake_understand(
        self, transcript: str, answers: str = "", mode: str = "add"
    ) -> dict:
        """Turn a spoken fridge description into reconciled, reviewable items.

        ``mode`` is ``"add"`` (stock new food) or ``"edit"`` (change food you
        already have: consume / toss / correct a date).
        """
        mode = "edit" if mode == "edit" else "add"
        catalog = self._catalog()
        try:
            if self.intake_understander is not None:
                result = self.intake_understander(
                    transcript=transcript, catalog=catalog, answers=answers, mode=mode
                )
            else:
                result = understand_transcript(
                    transcript,
                    settings=self.settings,
                    catalog=catalog,
                    answers=answers,
                    mode=mode,
                )
        except IntakeNotConfigured as exc:
            raise ApiError(503, str(exc)) from exc
        except IntakeError as exc:
            raise ApiError(502, str(exc)) from exc

        reconciled = reconcile_items(result.items, catalog, self.aliases)
        return {
            "items": [item.to_dict() for item in reconciled],
            "questions": result.questions,
            "summary": result.summary,
        }

    def intake_commit(self, items: List[dict]) -> dict:
        """Write the reviewed items to Grocy.

        Each item carries an ``action``: ``"add"`` (default) stocks it (creating
        the product if new); the edit actions act on an item you already have —
        ``"consume"`` books usage (undoable), ``"toss"`` waste-removes it, and
        ``"set_date"`` corrects its best-before date.
        """
        if not items:
            raise ApiError(400, "no items to commit")
        client = self._writer()

        # Master data (units/locations) is only needed when we might add/create.
        # The product index lets a new item whose name already exists in Grocy —
        # or that repeats within this same dump (two "Sonnenblumenöl", two
        # "Beluga Linsen") — reuse that product instead of creating a duplicate,
        # which Grocy rejects because product names are unique.
        resolvers: Optional[tuple] = None
        product_index: Optional[_ProductIndex] = None
        if any(_action_of(raw) == "add" for raw in items):
            resolvers = (
                _NameResolver(client.get_quantity_units(), self.settings.intake_default_unit),
                _NameResolver(client.get_locations(), self.settings.intake_default_location),
            )
            product_index = _ProductIndex(client.get_products(), self.aliases)

        results = []
        failed = []
        counts = {"added": 0, "created_products": 0, "changed": 0}
        for raw in items:
            action = _action_of(raw)
            try:
                if action == "consume":
                    results.append(self._commit_consume(client, raw, counts))
                elif action == "toss":
                    results.append(self._commit_toss(client, raw, counts))
                elif action == "set_date":
                    results.append(self._commit_set_date(client, raw, counts))
                else:
                    results.append(
                        self._commit_add(client, raw, resolvers, product_index, counts)
                    )
            except GrocyWriteDisabledError as exc:
                # Writes being off is a whole-client problem, not a per-item one;
                # nothing in this batch can succeed, so fail the request.
                raise ApiError(403, str(exc)) from exc
            except (GrocyClientError, ApiError) as exc:
                # One bad item must not sink the rest of a large dump: record it
                # and keep going. The UI reports the partial result + failures.
                name = str(raw.get("name") or "").strip()
                message = exc.message if isinstance(exc, ApiError) else str(exc)
                failed.append({"name": name or "?", "action": action, "error": message})

        return {
            "results": results,
            "failed": failed,
            "added": counts["added"],
            "created_products": counts["created_products"],
            "changed": counts["changed"],
        }

    # --- recipe inspiration ---------------------------------------------

    def recipe_ideas(
        self,
        mode: str = "stock",
        preferences: Optional[dict] = None,
        idea_model: Optional[str] = None,
        balance: Optional[float] = None,
        count: int = 8,
    ) -> dict:
        """Urgency-seeded dish headlines from current stock (+ optional shopping)."""
        self._require_intake()
        mode = "shop" if mode == "shop" else "stock"
        model = self._resolve_model(idea_model, self.settings.idea_model)
        balance = (
            self.settings.recipe_explore_balance if balance is None else float(balance)
        )
        seeds, inventory = self._seed_and_inventory()
        store = self._cook_path()
        gen = self.idea_generator or recipes_llm.generate_ideas
        try:
            result = gen(
                seeds=seeds,
                inventory=inventory,
                taste=cookmemory.taste_summary(store),
                recent_cooked=cookmemory.recent_cooked(store),
                mode=mode,
                preferences=preferences or {},
                balance=balance,
                count=count,
                model=model,
                settings=self.settings,
            )
        except (LlmError, LlmNotConfigured) as exc:
            raise self._llm_error(exc) from exc
        return {"mode": mode, "seeds": seeds, "ideas": result.get("ideas", [])}

    def recipe_chat(self, payload: Optional[dict] = None) -> dict:
        """Conversational recipe turn: reasons over the whole inventory.

        Body: ``{message, history:[{role,content}], preferences?, idea_model?}``.
        Returns ``{reply, ideas}`` where each idea shares the ``recipe_ideas``
        card shape, so the SPA reuses the same idea-card → recipe flow.
        """
        self._require_intake()
        payload = payload or {}
        message = str(payload.get("message") or "").strip()
        if not message:
            raise ApiError(400, "message is required")
        history = _clean_chat_history(payload.get("history"))
        preferences = payload.get("preferences")
        preferences = preferences if isinstance(preferences, dict) else {}
        model = self._resolve_model(payload.get("idea_model"), self.settings.idea_model)
        store = self._cook_path()
        gen = self.chat_generator or recipes_llm.chat_inventory
        try:
            result = gen(
                message=message,
                history=history,
                inventory_lines=self._inventory_lines(),
                taste=cookmemory.taste_summary(store),
                recent_cooked=cookmemory.recent_cooked(store),
                preferences=preferences,
                count=5,
                model=model,
                settings=self.settings,
            )
        except (LlmError, LlmNotConfigured) as exc:
            raise self._llm_error(exc) from exc
        return {"reply": result.get("reply", ""), "ideas": result.get("ideas", [])}

    def recipe_detail(
        self, idea: dict, mode: str = "stock", recipe_model: Optional[str] = None
    ) -> dict:
        """Turn a chosen idea into rough phase guidance (never numbered steps)."""
        self._require_intake()
        if not isinstance(idea, dict) or not str(idea.get("title") or "").strip():
            raise ApiError(400, "idea with a title is required")
        mode = "shop" if mode == "shop" else "stock"
        model = self._resolve_model(recipe_model, self.settings.recipe_model)
        gen = self.recipe_generator or recipes_llm.generate_recipe
        try:
            return gen(idea=idea, mode=mode, model=model, settings=self.settings)
        except (LlmError, LlmNotConfigured) as exc:
            raise self._llm_error(exc) from exc

    def recipe_twist(self, dish: str, transcript: str = "", text: str = "") -> dict:
        """Extract a 'Meine Version' twist, persist it, and merge its taste tags."""
        self._require_intake()
        dish = str(dish or "").strip()
        if not dish:
            raise ApiError(400, "dish is required")
        spoken = (transcript or text or "").strip()
        if not spoken:
            raise ApiError(400, "describe what you did differently")
        model = self.settings.recipe_model
        store = self._cook_path()
        gen = self.twist_extractor or recipes_llm.extract_twist
        try:
            twist = gen(transcript=spoken, dish=dish, model=model, settings=self.settings)
        except (LlmError, LlmNotConfigured) as exc:
            raise self._llm_error(exc) from exc
        cookmemory.add_twist(
            store,
            dish=dish,
            change=twist.get("change", ""),
            note=twist.get("note", ""),
            tags=twist.get("tags", {}),
        )
        return {"dish": dish, "twist": twist, "ok": True}

    def recipe_revise(
        self, recipe: dict, transcript: str = "", text: str = "", mode: str = "stock"
    ) -> dict:
        """'Meine Version': rewrite a recipe to match the user's changes.

        Folds the change into the recipe itself (regenerated guidance), updates
        the matching book entry in place (so it's not a near-duplicate), and
        persists the taste tags — the same learning signal :meth:`recipe_twist`
        wrote. The returned recipe carries ``twist`` so the cook-estimate flow
        can seed its consumption guess with the user's version.
        """
        self._require_intake()
        if not isinstance(recipe, dict) or not str(recipe.get("title") or "").strip():
            raise ApiError(400, "recipe with a title is required")
        spoken = (transcript or text or "").strip()
        if not spoken:
            raise ApiError(400, "describe what you did differently")
        mode = "shop" if mode == "shop" else "stock"
        original_title = str(recipe.get("title")).strip()
        model = self._resolve_model(None, self.settings.recipe_model)
        store = self._cook_path()
        extract = self.twist_extractor or recipes_llm.extract_twist
        reviser = self.recipe_reviser or recipes_llm.revise_recipe
        try:
            twist = extract(
                transcript=spoken, dish=original_title, model=model, settings=self.settings
            )
            revised = reviser(
                recipe=recipe, transcript=spoken, mode=mode, model=model, settings=self.settings
            )
        except (LlmError, LlmNotConfigured) as exc:
            raise self._llm_error(exc) from exc
        cookmemory.add_twist(
            store,
            dish=original_title,
            change=twist.get("change", ""),
            note=twist.get("note", ""),
            tags=twist.get("tags", {}),
        )
        revised["twist"] = spoken
        entry = cookmemory.upsert_book(
            store,
            match_title=original_title,
            title=revised.get("title") or original_title,
            guidance=revised.get("guidance", []),
            buy=revised.get("buy", []),
            twist=spoken,
        )
        return {"recipe": revised, "twist": twist, "entry": entry, "ok": True}

    def recipe_cooked(self, dish: str) -> dict:
        """Log a cooked dish so it's avoided in future idea generation."""
        dish = str(dish or "").strip()
        if not dish:
            raise ApiError(400, "dish is required")
        cookmemory.add_cooked(self._cook_path(), dish=dish)
        return {"dish": dish, "ok": True}

    def recipe_save(
        self, title: str, guidance: List[str], buy: Optional[List[str]] = None, twist: str = ""
    ) -> dict:
        """Save a recipe into the browsable 'Meine Rezepte' book."""
        title = str(title or "").strip()
        if not title:
            raise ApiError(400, "title is required")
        entry = cookmemory.add_to_book(
            self._cook_path(),
            title=title,
            guidance=guidance or [],
            buy=buy or [],
            twist=str(twist or ""),
        )
        return {"recipe": entry, "ok": True}

    def recipe_book(self) -> dict:
        """The saved-recipes book, newest first."""
        return {"recipes": cookmemory.book(self._cook_path())}

    # --- per-item emoji overrides ---------------------------------------

    def get_icons(self) -> dict:
        """The durable name->emoji override map for the SPA to apply on load."""
        return {"icons": cookmemory.get_icons(self._cook_path())}

    def set_icon(self, name: str, emoji: str) -> dict:
        """Attach (or clear, when ``emoji`` is empty) a symbol to a product name."""
        if not str(name or "").strip():
            raise ApiError(400, "name is required")
        return cookmemory.set_icon(self._cook_path(), name=name, emoji=emoji)

    # --- cook -> consumption tracking -----------------------------------

    def recipe_cook_estimate(
        self,
        dish: str,
        guidance: Optional[List[str]] = None,
        buy: Optional[List[str]] = None,
        mode: str = "stock",
        correction: str = "",
    ) -> dict:
        """Estimate what cooking ``dish`` consumed, as editable review rows.

        Consume rows (``kind:"consume"``) carry the matched product; bought rows
        (``kind:"bought"``, shop mode) carry a pack + used split. Names are
        resolved to Grocy products with :func:`reconcile_items` — the same
        machinery intake uses — so the user can edit before committing.
        """
        self._require_intake()
        dish = str(dish or "").strip()
        if not dish:
            raise ApiError(400, "dish is required")
        mode = "shop" if mode == "shop" else "stock"
        model = self._resolve_model(None, self.settings.recipe_model)

        stock = [item for item in self.stock_provider() if item.amount > 0]
        candidates = [
            {"name": item.name, "amount": item.amount, "unit": item.unit}
            for item in stock
            if item.name
        ]
        est = self.consumption_estimator or recipes_llm.estimate_consumption
        try:
            result = est(
                dish=dish,
                guidance=guidance or [],
                mode=mode,
                candidates=candidates,
                buy=buy or [],
                correction=str(correction or ""),
                model=model,
                settings=self.settings,
            )
        except (LlmError, LlmNotConfigured) as exc:
            raise self._llm_error(exc) from exc

        catalog = self._catalog()
        stock_by_pid = {item.product_id: item for item in stock}
        rows: List[dict] = []
        used = result.get("used") if isinstance(result.get("used"), list) else []
        as_items = [
            IntakeItem(
                name=str(u.get("name") or ""),
                quantity=_safe_float(u.get("amount"), 1.0),
                unit=u.get("unit"),
                action="consume",
            )
            for u in used
            if isinstance(u, dict) and str(u.get("name") or "").strip()
        ]
        for item in reconcile_items(as_items, catalog, self.aliases):
            row = item.to_dict()
            row["kind"] = "consume"
            row["amount"] = row.pop("quantity")
            # Attach the matched product's CURRENT stock so the review can show
            # "X used of Y in stock -> Z left" and let the user sanity-check both
            # the amount and whether the right inventory entry was matched.
            stocked = stock_by_pid.get(row.get("matched_product_id"))
            row["stock_amount"] = stocked.amount if stocked else None
            row["stock_unit"] = stocked.unit if stocked else None
            rows.append(row)

        # Ingredients the user explicitly named but that aren't in stock: shown
        # as explained, non-committing rows so a correction like "I used olive
        # oil" doesn't silently vanish when no olive oil is tracked.
        missing = result.get("missing") if isinstance(result.get("missing"), list) else []
        for m in missing:
            if not isinstance(m, dict) or not str(m.get("name") or "").strip():
                continue
            rows.append(
                {
                    "kind": "consume",
                    "name": str(m.get("name")).strip(),
                    "amount": _safe_float(m.get("amount"), 1.0),
                    "unit": m.get("unit"),
                    "matched_product_id": None,
                    "match": "missing",
                    "stock_amount": None,
                    "stock_unit": None,
                }
            )

        bought = result.get("bought") if isinstance(result.get("bought"), list) else []
        for b in bought if mode == "shop" else []:
            if not isinstance(b, dict) or not str(b.get("name") or "").strip():
                continue
            rows.append(
                {
                    "kind": "bought",
                    "name": str(b.get("name")).strip(),
                    "unit": b.get("unit"),
                    "pack_amount": _safe_float(b.get("pack_amount"), 1.0),
                    "used_amount": _safe_float(b.get("used_amount"), 1.0),
                    "matched_product_id": None,
                    "match": "new",
                }
            )
        return {"dish": dish, "mode": mode, "items": rows}

    def recipe_cook_commit(self, dish: str, items: List[dict]) -> dict:
        """Book a cooked dish's consumption to Grocy and persist the session.

        consume rows are removed from stock; bought rows add the full pack then
        deduct the used amount (the leftover stays in the fridge). Per-item
        resilience mirrors :meth:`intake_commit` — one bad row is recorded in
        ``failed`` and the rest proceed. The session is stored so each line stays
        correctable later in the Verlauf.
        """
        dish = str(dish or "").strip()
        if not dish:
            raise ApiError(400, "dish is required")
        if not items:
            raise ApiError(400, "no items to commit")
        client = self._writer()

        resolvers: Optional[tuple] = None
        product_index: Optional[_ProductIndex] = None
        if any(_cook_kind(raw) == "bought" for raw in items):
            resolvers = (
                _NameResolver(client.get_quantity_units(), self.settings.intake_default_unit),
                _NameResolver(client.get_locations(), self.settings.intake_default_location),
            )
            product_index = _ProductIndex(client.get_products(), self.aliases)

        lines: List[dict] = []
        failed: List[dict] = []
        counts = {"added": 0, "consumed": 0}
        for raw in items:
            kind = _cook_kind(raw)
            name = str(raw.get("name") or "").strip()
            try:
                if kind == "bought":
                    lines.append(
                        self._cook_commit_bought(client, raw, resolvers, product_index, counts)
                    )
                else:
                    lines.append(self._cook_commit_consume(client, raw, counts))
            except GrocyWriteDisabledError as exc:
                raise ApiError(403, str(exc)) from exc
            except (GrocyClientError, ApiError) as exc:
                message = exc.message if isinstance(exc, ApiError) else str(exc)
                failed.append({"name": name or "?", "kind": kind, "error": message})

        cookmemory.add_cooked(self._cook_path(), dish=dish)
        session = cookmemory.add_session(self._cook_path(), dish=dish, lines=lines)
        return {
            "session_id": session["id"],
            "dish": dish,
            "added": counts["added"],
            "consumed": counts["consumed"],
            "lines": lines,
            "failed": failed,
            "ok": True,
        }

    def cook_history(self) -> dict:
        """Past cooking sessions, newest first."""
        return {"sessions": cookmemory.sessions(self._cook_path())}

    def cook_adjust(self, session_id: str, line_index: int, new_amount: float) -> dict:
        """Correct one consumed line: undo the old booking and re-book ``new_amount``.

        A correction first reverses the original Grocy transaction, then (if the
        new amount is positive) consumes that amount fresh, recomputing whether
        the product is now depleted. The stored session line is updated so the
        Verlauf always reflects what's actually in Grocy.
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ApiError(400, "session_id is required")
        try:
            new_amount = float(new_amount)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "new_amount must be a number") from exc
        if new_amount < 0:
            raise ApiError(400, "new_amount cannot be negative")

        sessions = cookmemory.sessions(self._cook_path())
        session = next((s for s in sessions if s.get("id") == session_id), None)
        if session is None:
            raise ApiError(404, f"no cooking session {session_id!r}")
        lines = session.get("lines") or []
        if not (0 <= line_index < len(lines)):
            raise ApiError(400, f"line {line_index} is out of range")
        line = lines[line_index]
        product_id = str(line.get("product_id") or "")
        if not product_id:
            raise ApiError(400, "this line has no product to adjust")

        client = self._writer()
        try:
            old_txn = line.get("transaction_id")
            if old_txn:
                undo(client, old_txn)
            new_txn = None
            if new_amount > 0:
                outcome = consume(client, product_id, new_amount)
                new_txn = outcome.undo_transaction_id
                new_amount = outcome.amount
            depleted = _live_stock_amount(client, product_id) <= 0
        except GrocyWriteDisabledError as exc:
            raise ApiError(403, str(exc)) from exc
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc

        cookmemory.update_session_line(
            self._cook_path(),
            session_id,
            line_index,
            amount=new_amount,
            transaction_id=new_txn,
            depleted=depleted,
        )
        return {
            "session_id": session_id,
            "line_index": line_index,
            "amount": new_amount,
            "depleted": depleted,
            "ok": True,
        }

    def recipe_cook_undo(self, session_id: str) -> dict:
        """Reverse a whole cooking session — the "rückgängig machen" on the result.

        For each booked line this undoes the consume (restoring what the dish
        used) and, for a bought row, also the purchase (removing the added pack),
        leaving stock as it was before "Verbucht". On full success the session is
        dropped from the Verlauf; if any line fails the session is kept so it can
        be retried, and the failures are returned. A newly created product stays
        in Grocy with 0 stock (Grocy keeps products; only the booking is undone).
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ApiError(400, "session_id is required")
        session = next(
            (
                s
                for s in cookmemory.sessions(self._cook_path())
                if s.get("id") == session_id
            ),
            None,
        )
        if session is None:
            raise ApiError(404, f"no cooking session {session_id!r}")

        client = self._writer()
        reversed_count = 0
        failed: List[dict] = []
        for line in session.get("lines") or []:
            txns = [t for t in (line.get("transaction_id"), line.get("add_transaction_id")) if t]
            if not txns:
                continue
            try:
                for txn in txns:
                    undo(client, txn)
                reversed_count += 1
            except GrocyWriteDisabledError as exc:
                raise ApiError(403, str(exc)) from exc
            except GrocyClientError as exc:
                failed.append({"name": line.get("name") or "?", "error": str(exc)})

        removed = False
        if not failed:
            removed = cookmemory.remove_session(self._cook_path(), session_id) is not None
        return {
            "ok": not failed,
            "session_id": session_id,
            "dish": session.get("dish") or "",
            "reversed": reversed_count,
            "removed": removed,
            "failed": failed,
        }

    def recipe_add_missing(
        self,
        name: str,
        amount: float = 1.0,
        unit: Optional[str] = None,
        location: Optional[str] = None,
        used: float = 0.0,
    ) -> dict:
        """Create a cook-review "missing" ingredient in Grocy as tracked stock.

        Adds an initial amount, then — if ``used`` > 0 — immediately deducts
        what the dish consumed so the leftover stays in the fridge (same
        create → add_stock → consume path as a bought cook row, but standalone
        so it does not open a cook session). Reuses an existing product of the
        same name rather than creating a duplicate.
        """
        name = str(name or "").strip()
        if not name:
            raise ApiError(400, "name is required")
        try:
            initial = float(amount)
        except (TypeError, ValueError):
            initial = 1.0  # non-numeric -> sensible default
        if initial <= 0:
            raise ApiError(400, "amount must be greater than zero")
        used_amt = min(max(_safe_float(used, 0.0), 0.0), initial)
        client = self._writer()
        units = _NameResolver(
            client.get_quantity_units(), self.settings.intake_default_unit
        )
        locations = _NameResolver(
            client.get_locations(), self.settings.intake_default_location
        )
        product_index = _ProductIndex(client.get_products(), self.aliases)
        loc = location if location is not None else self.settings.intake_default_location
        location_id = locations.resolve(loc)

        try:
            product_id = product_index.resolve(name)
            created = False
            if not product_id:
                product_id = client.create_product(
                    name, qu_id_stock=units.resolve(unit), location_id=location_id
                )
                created = True
            client.add_stock(product_id, initial, location_id=location_id)
            txn = None
            depleted = False
            if used_amt > 0:
                outcome = consume(client, product_id, used_amt)
                txn = outcome.undo_transaction_id
                used_amt = outcome.amount
                depleted = _live_stock_amount(client, product_id) <= 0
        except GrocyWriteDisabledError as exc:
            raise ApiError(403, str(exc)) from exc
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc

        return {
            "ok": True,
            "name": name,
            "product_id": product_id,
            "created": created,
            "amount": initial,
            "used": used_amt,
            "unit": unit or None,
            "location": loc or None,
            "transaction_id": txn,
            "depleted": depleted,
        }

    def _cook_commit_consume(self, client, raw: dict, counts: dict) -> dict:
        name = str(raw.get("name") or "").strip()
        product_id = _require_known_product(raw, "use")
        amount = _amount_of(raw)
        outcome = consume(client, product_id, amount)
        depleted = _live_stock_amount(client, product_id) <= 0
        counts["consumed"] += 1
        return {
            "name": name or product_id,
            "product_id": product_id,
            "amount": outcome.amount,
            "unit": raw.get("unit") or None,
            "transaction_id": outcome.undo_transaction_id,
            "depleted": depleted,
            "kind": "consume",
        }

    def _cook_commit_bought(
        self, client, raw: dict, resolvers, product_index, counts: dict
    ) -> dict:
        assert resolvers is not None  # built whenever a bought row is present
        units, locations = resolvers
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ApiError(400, "a bought item needs a name")
        pack = _safe_float(raw.get("pack_amount"), 1.0)
        used = _safe_float(raw.get("used_amount"), pack)
        if pack <= 0:
            raise ApiError(400, "pack amount must be greater than zero")
        used = min(max(used, 0.0), pack)
        location_id = locations.resolve(raw.get("location"))

        product_id = _pid(raw)
        if not product_id:
            existing_id = product_index.resolve(name) if product_index else ""
            if existing_id:
                product_id = existing_id
            else:
                product_id = client.create_product(
                    name,
                    qu_id_stock=units.resolve(raw.get("unit")),
                    location_id=location_id,
                )
                if product_index is not None:
                    product_index.add(name, product_id)
        add_txn = _extract_txn(client.add_stock(product_id, pack, location_id=location_id))
        counts["added"] += 1
        txn = None
        depleted = False
        if used > 0:
            outcome = consume(client, product_id, used)
            txn = outcome.undo_transaction_id
            counts["consumed"] += 1
            depleted = _live_stock_amount(client, product_id) <= 0
        return {
            "name": name,
            "product_id": product_id,
            "amount": used,
            "unit": raw.get("unit") or None,
            "transaction_id": txn,
            "add_transaction_id": add_txn,
            "depleted": depleted,
            "kind": "bought",
            "pack_amount": pack,
        }

    # recipe helpers

    def _seed_and_inventory(self):
        """Split current stock into urgent seeds (hot+warm) and supporting inventory."""
        today = self.today_provider()
        window = self.settings.expiry_window_days
        scored = [
            score_stock_item(item, today=today, expiry_window_days=window)
            for item in self.stock_provider()
            if item.amount > 0
        ]
        scored.sort(key=lambda u: (-u.urgency_score, u.item.name.lower()))
        seeds, inventory = [], []
        for urgency in scored:
            band = band_for(urgency.days_until_expiry, window)
            (seeds if band in ("hot", "warm") else inventory).append(urgency.item.name)
        return seeds, inventory

    def _inventory_lines(self) -> List[str]:
        """The whole stock as compact annotated lines for the chat prompt.

        Each line carries name, amount/unit, location, and (when known) the
        relative expiry — e.g. ``"Sauerkraut — 500 g (Kühlschrank, läuft in 2
        Tagen ab)"`` — sorted urgent-first so the model leads with what to save.
        """
        today = self.today_provider()
        window = self.settings.expiry_window_days
        scored = [
            score_stock_item(item, today=today, expiry_window_days=window)
            for item in self.stock_provider()
            if item.amount > 0
        ]
        scored.sort(key=lambda u: (-u.urgency_score, u.item.name.lower()))
        return [_inventory_line(u) for u in scored]

    def _cook_path(self):
        if self.cook_store_path is not None:
            return self.cook_store_path
        data_dir = getattr(self.settings, "data_dir", "data") or "data"
        return Path(data_dir) / "cookmemory.json"

    def _resolve_model(self, requested: Optional[str], default: str) -> str:
        requested = str(requested or "").strip()
        if not requested:
            return default
        if not recipes_llm.is_valid_model(requested):
            raise ApiError(400, f"unknown model {requested!r}")
        return requested

    def _require_intake(self) -> None:
        if (
            self.idea_generator
            or self.recipe_generator
            or self.twist_extractor
            or self.consumption_estimator
            or self.chat_generator
            or self.diet_suggester
        ):
            return  # injected generators don't need a real key (tests)
        if not getattr(self.settings, "intake_enabled", False):
            raise ApiError(503, "recipe inspiration needs FOODBRAIN_OPENROUTER_API_KEY")

    def _llm_error(self, exc) -> "ApiError":
        if isinstance(exc, LlmNotConfigured):
            return ApiError(503, str(exc))
        return ApiError(502, str(exc))

    def _commit_add(self, client, raw: dict, resolvers, product_index, counts: dict) -> dict:
        assert resolvers is not None  # built whenever an add is present
        units, locations = resolvers
        name = str(raw.get("name") or "").strip()
        product_id = _pid(raw)
        amount = _amount_of(raw)
        best_before = _optional_iso_date(raw.get("best_before_date"))
        location_id = locations.resolve(raw.get("location"))
        was_created = False
        if not product_id:
            if not name:
                raise ApiError(400, "a new item needs a name")
            # Reuse an existing/just-created product of the same name (Grocy
            # product names are unique); only create when truly new.
            existing_id = product_index.resolve(name) if product_index else ""
            if existing_id:
                product_id = existing_id
            else:
                product_id = client.create_product(
                    name,
                    qu_id_stock=units.resolve(raw.get("unit")),
                    location_id=location_id,
                )
                if product_index is not None:
                    product_index.add(name, product_id)
                was_created = True
                counts["created_products"] += 1
        add_response = client.add_stock(
            product_id, amount, best_before_date=best_before, location_id=location_id
        )
        if raw.get("opened"):
            client.open_product(product_id, amount)
        counts["added"] += 1
        return {
            "name": name or product_id,
            "product_id": product_id,
            "action": "add",
            "created": was_created,
            "amount": amount,
            "best_before_date": best_before.isoformat() if best_before else None,
            "transaction_id": _extract_txn(add_response),
            "ok": True,
        }

    def _commit_consume(self, client, raw: dict, counts: dict) -> dict:
        name = str(raw.get("name") or "").strip()
        product_id = _require_known_product(raw, "use")
        amount = _amount_of(raw)
        outcome = consume(client, product_id, amount)
        counts["changed"] += 1
        return {
            "name": name or product_id,
            "product_id": product_id,
            "action": "consume",
            "amount": amount,
            "transaction_id": outcome.undo_transaction_id,
            "ok": True,
        }

    def _commit_toss(self, client, raw: dict, counts: dict) -> dict:
        name = str(raw.get("name") or "").strip()
        product_id = _require_known_product(raw, "toss")
        amount = _amount_of(raw)
        outcome = toss(client, product_id, amount, confirm=True)
        counts["changed"] += 1
        return {
            "name": name or product_id,
            "product_id": product_id,
            "action": "toss",
            "amount": amount,
            "transaction_id": outcome.undo_transaction_id,
            "ok": True,
        }

    def _commit_set_date(self, client, raw: dict, counts: dict) -> dict:
        name = str(raw.get("name") or "").strip()
        product_id = _require_known_product(raw, "re-date")
        best_before = _optional_iso_date(raw.get("best_before_date"))
        if best_before is None:
            raise ApiError(400, f"need a date to set for {name or product_id!r}")
        entries = client.get_product_entries(product_id)
        if not entries:
            raise ApiError(502, f"{name or product_id!r} has no stock entries to date")
        set_due_date(
            client, _next_due_entry(entries).stock_entry_id, best_before, product_id=product_id
        )
        counts["changed"] += 1
        return {
            "name": name or product_id,
            "product_id": product_id,
            "action": "set_date",
            "best_before_date": best_before.isoformat(),
            "ok": True,
        }

    def _catalog(self) -> List[dict]:
        if self.product_catalog_provider is not None:
            return self.product_catalog_provider()
        # Sample / JSON modes have no product master list; fall back to the
        # in-stock names so the UI still demos and can match what's on hand.
        return [
            {"id": item.product_id, "name": item.name}
            for item in self.stock_provider()
            if item.name
        ]

    # --- internals -------------------------------------------------------

    def _connect_pairings(
        self, selected_urgencies: List[IngredientUrgency], stock: List[StockItem]
    ) -> list:
        if self.pairings is None:
            return []
        suggestions = suggest_pairings(
            self.pairings,
            selected_urgencies,
            stock,
            ingredient_limit=len(selected_urgencies),
            partner_limit=self.settings.pairing_browse_limit,
            aliases=self.aliases,
        )
        return [
            {
                "ingredient": suggestion.ingredient,
                "urgency_score": suggestion.urgency_score,
                "partners": [
                    {
                        "name": partner.name,
                        "score": partner.score,
                        "in_stock": partner.in_stock,
                    }
                    for partner in suggestion.partners
                ],
            }
            for suggestion in suggestions
        ]

    def _connect_recipes(
        self,
        selected: List[StockItem],
        stock: List[StockItem],
        today: date,
        window: int,
    ) -> list:
        if not self.recipes:
            return []
        unlocked = _recipes_using(self.recipes, selected, self.aliases)
        matches = rank_recipes(
            unlocked,
            stock,
            today=today,
            expiry_window_days=window,
            limit=self.settings.top_recipe_limit,
            aliases=self.aliases,
        )
        return [
            {
                "name": match.recipe.name,
                "coverage": match.coverage,
                "expiry_usefulness": match.expiry_usefulness,
                "score": match.score,
                "matched": [ing.name for ing in match.matched],
                "missing": [ing.name for ing in match.missing],
            }
            for match in matches
        ]

    def _write(
        self,
        action: Callable[[GrocyClient], WriteOutcome],
        *,
        after: Optional[Callable[[GrocyClient, WriteOutcome], None]] = None,
    ) -> dict:
        client = self._writer()
        try:
            outcome = action(client)
        except ConfirmationRequired as exc:
            raise ApiError(409, str(exc)) from exc
        except GrocyWriteDisabledError as exc:
            raise ApiError(403, str(exc)) from exc
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc
        if after is not None:
            after(client, outcome)
        return _serialize_outcome(outcome)

    def _writer(self) -> GrocyClient:
        if self.write_client_factory is None:
            raise ApiError(
                403, "writes are disabled: no writable Grocy client is configured"
            )
        return self.write_client_factory()

    def _reader(self) -> GrocyClient:
        # Reads also need a Grocy client; reuse the write factory if present,
        # otherwise a read-only one cannot be built without credentials.
        if self.write_client_factory is not None:
            return self.write_client_factory()
        raise ApiError(
            503, "no Grocy client configured for live reads (entries lookup)"
        )


def _serialize_scored_item(urgency: IngredientUrgency, window: int) -> dict:
    item = urgency.item
    return {
        "product_id": item.product_id,
        "name": item.name,
        "amount": item.amount,
        "unit": item.unit,
        "location": item.location,
        "best_before_date": item.best_before_date.isoformat()
        if item.best_before_date
        else None,
        "days_until_expiry": urgency.days_until_expiry,
        "urgency_score": urgency.urgency_score,
        "reason": urgency.reason,
        "band": band_for(urgency.days_until_expiry, window),
    }


def _serialize_outcome(outcome: WriteOutcome) -> dict:
    return {
        "action": outcome.action,
        "product_id": outcome.product_id,
        "amount": outcome.amount,
        "transaction_id": outcome.undo_transaction_id,
        "undoable": outcome.undoable,
        "detail": outcome.detail,
        "ok": True,
    }


def _resolve_selection(
    stock: List[StockItem], selection: List[str]
) -> List[StockItem]:
    """Map a list of product ids (the SPA's selection) to live stock items.

    Unknown ids are silently skipped; order follows the request so the prompt
    and pairings read in the order the user tapped.
    """
    by_id = {item.product_id: item for item in stock if item.amount > 0}
    resolved: List[StockItem] = []
    seen = set()
    for raw in selection:
        key = str(raw)
        item = by_id.get(key)
        if item is not None and key not in seen:
            resolved.append(item)
            seen.add(key)
    return resolved


def _recipes_using(
    recipes: List[Recipe],
    selected: List[StockItem],
    aliases: Optional[Dict[str, str]],
) -> List[Recipe]:
    """Keep only recipes that call for at least one selected ingredient.

    Uses the same token-containment heuristic as recipe matching so "Zucchini"
    in stock unlocks a recipe ingredient line "2 zucchini, sliced".
    """
    selected_tokens = [_recipe_tokenize(item.name, aliases) for item in selected]
    selected_tokens = [tokens for tokens in selected_tokens if tokens]

    using: List[Recipe] = []
    for recipe in recipes:
        for ingredient in recipe.ingredients:
            ing_tokens = _recipe_tokenize(ingredient.name, aliases)
            if any(_tokens_match(ing_tokens, sel) for sel in selected_tokens):
                using.append(recipe)
                break
    return using


def _describe_item(item: StockItem) -> str:
    amount = f"{item.amount:g}"
    if item.unit:
        return f"{amount} {item.unit} {item.name}"
    return f"{amount} {item.name}"


def _inventory_line(urgency: IngredientUrgency) -> str:
    """One annotated stock line for the chat prompt: name — amount (place, expiry)."""
    item = urgency.item
    amount = f"{item.amount:g}{(' ' + item.unit) if item.unit else ''}".strip()
    qualifiers = []
    if item.location:
        qualifiers.append(str(item.location))
    qualifiers.append(_expiry_phrase(urgency.days_until_expiry))
    head = f"{item.name} — {amount}" if amount else item.name
    return f"{head} ({', '.join(q for q in qualifiers if q)})"


def _expiry_phrase(days: Optional[int]) -> str:
    if days is None:
        return "ohne Ablaufdatum"
    if days < 0:
        return f"seit {abs(days)} Tagen überfällig"
    if days == 0:
        return "läuft heute ab"
    if days == 1:
        return "läuft morgen ab"
    return f"läuft in {days} Tagen ab"


def _clean_chat_history(history) -> List[dict]:
    """Sanitize incoming chat history into well-formed user/assistant turns."""
    cleaned: List[dict] = []
    if not isinstance(history, list):
        return cleaned
    for entry in history:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = str(entry.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            cleaned.append({"role": role, "content": content})
    return cleaned


# --- "Ask-AI" German prompt builder ------------------------------------------
# The SPA collects a short "food mood" (cuisine / style / special needs) and we
# turn it into an editable German prompt. Keys are stable ids the frontend sends;
# values are the natural-language fragments woven into the prompt. Unknown ids are
# ignored so the UI and API can evolve independently.

_CUISINES = {
    "asiatisch": "asiatische",
    "europäisch": "europäische",
    "fusion": "Fusion-",
    "gesund": "gesunde, nährstoffreiche",
    "wohlfühl": "Wohlfühl- / Soulfood-",
    "egal": "",  # no cuisine constraint -> more variety (see _prompt_count)
}

_STYLES = {
    "einfach": "Halte sie super einfach und anfängerfreundlich, mit wenigen Schritten.",
    "experimentell": "Sei ruhig experimentell und kreativ — überrasch mich.",
    "schnell": "Jedes Gericht soll in etwa 30 Minuten fertig sein.",
    "gäste": "Sie sollen sich für ein Abendessen mit Freunden eignen — etwas zum Vorzeigen.",
}

_NEEDS = {
    "warm": "warm",
    "kalt": "kalt",
    "sättigend": "sättigend",
    "leicht": "leicht",
    "scharf": "scharf gewürzt",
    "mild": "mild gewürzt",
}


def _prompt_count(cuisine: str) -> int:
    """How many dinners to ask for. 'egal' widens the net for more variety."""
    return 8 if cuisine == "egal" else 3


def _join_de(parts: List[str]) -> str:
    """German list join: 'a', 'a und b', 'a, b und c'."""
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} und {parts[-1]}"


def _german_prompt(listed: str, preferences: dict) -> str:
    cuisine = str(preferences.get("cuisine") or "").strip().lower()
    style = str(preferences.get("style") or "").strip().lower()
    raw_needs = preferences.get("needs") or []
    if isinstance(raw_needs, str):
        raw_needs = [raw_needs]
    needs = [_NEEDS[n] for n in (str(x).strip().lower() for x in raw_needs) if n in _NEEDS]

    count = _prompt_count(cuisine)
    cuisine_word = _CUISINES.get(cuisine, "")
    kind = f"{cuisine_word} Gerichte" if cuisine_word else "Gerichte"

    sentences = [
        f"Ich habe {listed}.",
        f"Schlag mir {count} {kind} vor, die ich heute kochen kann — "
        "hauptsächlich mit diesen Zutaten plus üblichen Vorratssachen.",
    ]
    if cuisine == "egal":
        sentences.append("Mix gern quer durch verschiedene Küchen für mehr Abwechslung.")
    if needs:
        sentences.append(f"Die Gerichte sollen {_join_de(needs)} sein.")
    if style in _STYLES:
        sentences.append(_STYLES[style])
    sentences.append("Sag mir bei jedem Gericht grob, wie lange es dauert.")
    return " ".join(sentences)


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ApiError(400, f"best_before_date must be ISO YYYY-MM-DD, got {value!r}") from exc


def _optional_iso_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    return _parse_iso_date(str(value))


def _next_due_entry(entries):
    """The stock entry whose best-before the aggregated stock view displays.

    Grocy's ``/entries`` endpoint returns opened stock first (its consume
    order), NOT earliest-first, so ``entries[0]`` can be a later-dated opened
    entry while the stock tile shows an earlier unopened one. The tile's date
    is the *minimum* best_before across entries, so a re-date must target that
    entry or the visible date never moves. Dateless entries sort last; if none
    has a date, fall back to the first entry.
    """
    return min(entries, key=lambda e: e.best_before_date or date.max)


def _amount_of(raw: dict) -> float:
    value = raw.get("amount", raw.get("quantity", 1.0))
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"amount must be a number, got {value!r}") from exc
    if amount <= 0:
        raise ApiError(400, "amount must be greater than zero")
    return amount


def _extract_txn(response) -> Optional[str]:
    return extract_transaction_id(response)


def _truthy(value) -> bool:
    """Grocy booleans travel as ``"0"``/``"1"`` (sometimes real bools) — normalize."""
    return str(value).strip().lower() in ("1", "true")


# Low-quantity threshold: below this fraction of the learned typical amount,
# the item is flagged even though it isn't at zero yet.
_LOW_QTY_FRACTION = 0.34


def _shopping_signal(
    habit: dict, stats: dict, current_amount: float, unit: Optional[str], now: float
) -> tuple:
    """The single best-ranked reason to suggest a habit's product, or (None, "")."""
    removals = habit.get("removals") or []
    if current_amount <= 0 and removals:
        last_removed = _parse_habit_ts(removals[-1].get("ts"))
        if last_removed is not None:
            days = max(0, round((now - last_removed) / 86400))
            reason = "gerade aufgebraucht" if days <= 1 else f"vor {days} Tagen aufgebraucht"
        else:
            reason = "aufgebraucht"
        return "depleted", reason

    typical = stats.get("typical_amount")
    if typical and current_amount > 0 and current_amount < typical * _LOW_QTY_FRACTION:
        unit_part = f" {unit}" if unit else ""
        return "low_qty", f"nur noch {current_amount:g}{unit_part} übrig"

    interval = stats.get("median_interval_days")
    last_buy = stats.get("last_buy_ts")
    if interval and last_buy is not None and (now - last_buy) / 86400 >= interval:
        return "interval", f"kaufst du ~alle {round(interval)} Tage"

    return None, ""


def _parse_habit_ts(value) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, TypeError):
        return None


def _shopping_rev(rows: List[dict], overlay: dict) -> str:
    """Short content hash of the Grocy rows + overlay, so a poller only re-renders on change."""
    payload = json.dumps({"rows": rows, "overlay": overlay}, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


_EDIT_ACTIONS = {"consume", "toss", "set_date"}


def _action_of(raw: dict) -> str:
    """The commit action for an item; anything but a known edit action is 'add'."""
    action = str(raw.get("action") or "add").strip().lower()
    return action if action in _EDIT_ACTIONS else "add"


def _cook_kind(raw: dict) -> str:
    """A cook-commit row is a 'bought' (add pack + deduct) or a plain 'consume'."""
    return "bought" if str(raw.get("kind") or "").strip().lower() == "bought" else "consume"


# Intentionally NOT shared with the other float coercers (_num, _as_float,
# _to_float, _float_setting): they differ on default/>0-gating/None semantics,
# so unifying them would change behavior. This one gates on > 0.
def _safe_float(value, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _pid(raw: dict) -> str:
    return str(raw.get("matched_product_id") or raw.get("product_id") or "")


def _require_known_product(raw: dict, verb: str) -> str:
    """An edit action can only target a product already in Grocy."""
    product_id = _pid(raw)
    if not product_id:
        name = str(raw.get("name") or "that").strip() or "that"
        raise ApiError(400, f"can't {verb} {name!r}: it isn't in your fridge")
    return product_id


class _ProductIndex:
    """Map a product name to its Grocy id, by normalized name.

    Seeded from the live product master list and extended as products are
    created during a commit, so a repeated name (within the dump or already in
    Grocy) reuses the existing product rather than creating a duplicate — Grocy
    enforces unique product names, so a duplicate ``create_product`` 400s.
    """

    def __init__(self, products: List[dict], aliases: Optional[Dict[str, str]]) -> None:
        self._aliases = aliases
        self._by_norm: Dict[str, str] = {}
        for product in products:
            self.add(str(product.get("name") or ""), str(product.get("id") or ""))

    def add(self, name: str, product_id: str) -> None:
        norm = normalize_ingredient_name(name, self._aliases)
        if norm and product_id:
            self._by_norm.setdefault(norm, product_id)

    def resolve(self, name) -> str:
        norm = normalize_ingredient_name(str(name or ""), self._aliases)
        return self._by_norm.get(norm, "")


class _NameResolver:
    """Resolve a free-text unit/location name to a Grocy object id.

    Order: exact normalized match -> configured default name -> first object.
    Built once per commit so master-data is fetched at most once each.
    """

    def __init__(self, objects: List[dict], default_name: Optional[str]) -> None:
        self._objects = [o for o in objects if o.get("id")]
        self._by_norm = {
            normalize_ingredient_name(str(o.get("name") or "")): str(o["id"])
            for o in self._objects
            if o.get("name")
        }
        self._default_name = default_name

    def resolve(self, name) -> str:
        candidate = normalize_ingredient_name(str(name or ""))
        if candidate and candidate in self._by_norm:
            return self._by_norm[candidate]
        if self._default_name:
            default_norm = normalize_ingredient_name(self._default_name)
            if default_norm in self._by_norm:
                return self._by_norm[default_norm]
        if self._objects:
            return str(self._objects[0]["id"])
        raise ApiError(
            502, "Grocy has no quantity units / locations to assign a new product to"
        )
