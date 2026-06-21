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
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import cookmemory, recipes_llm
from .grocy_client import GrocyClient, GrocyClientError, GrocyWriteDisabledError
from .llm import LlmError, LlmNotConfigured
from .intake import (
    IntakeError,
    IntakeItem,
    IntakeNotConfigured,
    IntakeResult,
    reconcile_items,
    understand_transcript,
)
from .matching import _tokenize as _recipe_tokenize
from .matching import _tokens_match, rank_recipes
from .models import IngredientUrgency, Recipe, StockItem
from .normalization import normalize_ingredient_name
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
    twist_extractor: Optional[Callable[..., dict]] = None
    consumption_estimator: Optional[Callable[..., dict]] = None
    cook_store_path: Optional[object] = None  # str | Path

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
        return self._write(lambda client: consume(client, product_id, amount))

    def toss(self, product_id: str, amount: float = 1.0, *, confirm: bool = False) -> dict:
        return self._write(
            lambda client: toss(client, product_id, amount, confirm=confirm)
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
            rows.append(row)

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
        client.add_stock(product_id, pack, location_id=location_id)
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
            client, entries[0].stock_entry_id, best_before, product_id=product_id
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

    def _write(self, action: Callable[[GrocyClient], WriteOutcome]) -> dict:
        client = self._writer()
        try:
            outcome = action(client)
        except ConfirmationRequired as exc:
            raise ApiError(409, str(exc)) from exc
        except GrocyWriteDisabledError as exc:
            raise ApiError(403, str(exc)) from exc
        except GrocyClientError as exc:
            raise ApiError(502, str(exc)) from exc
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
    from .grocy_client import extract_transaction_id

    return extract_transaction_id(response)


_EDIT_ACTIONS = {"consume", "toss", "set_date"}


def _action_of(raw: dict) -> str:
    """The commit action for an item; anything but a known edit action is 'add'."""
    action = str(raw.get("action") or "add").strip().lower()
    return action if action in _EDIT_ACTIONS else "add"


def _cook_kind(raw: dict) -> str:
    """A cook-commit row is a 'bought' (add pack + deduct) or a plain 'consume'."""
    return "bought" if str(raw.get("kind") or "").strip().lower() == "bought" else "consume"


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
