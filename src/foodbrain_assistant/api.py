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
from typing import Callable, Dict, List, Optional

from .grocy_client import GrocyClient, GrocyClientError, GrocyWriteDisabledError
from .intake import (
    IntakeError,
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
    consume,
    set_due_date,
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
        }

    def build_prompt(self, selection: List[str]) -> dict:
        """An editable LLM prompt prefilled from the selection (no LLM call)."""
        stock = self.stock_provider()
        selected = _resolve_selection(stock, selection)
        if not selected:
            raise ApiError(400, "selection did not match any in-stock products")

        listed = ", ".join(_describe_item(item) for item in selected)
        prompt = (
            f"I have {listed}. Suggest 3 simple dinners I can make tonight using "
            "mostly these, plus common pantry staples. Keep them quick and "
            "beginner-friendly, and tell me roughly how long each takes."
        )
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
        resolvers: Optional[tuple] = None
        if any(_action_of(raw) == "add" for raw in items):
            resolvers = (
                _NameResolver(client.get_quantity_units(), self.settings.intake_default_unit),
                _NameResolver(client.get_locations(), self.settings.intake_default_location),
            )

        results = []
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
                    results.append(self._commit_add(client, raw, resolvers, counts))
            except GrocyWriteDisabledError as exc:
                raise ApiError(403, str(exc)) from exc
            except GrocyClientError as exc:
                name = str(raw.get("name") or "")
                raise ApiError(502, f"failed to update {name or '?'!r}: {exc}") from exc

        return {
            "results": results,
            "added": counts["added"],
            "created_products": counts["created_products"],
            "changed": counts["changed"],
        }

    def _commit_add(self, client, raw: dict, resolvers, counts: dict) -> dict:
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
            product_id = client.create_product(
                name,
                qu_id_stock=units.resolve(raw.get("unit")),
                location_id=location_id,
            )
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
            partner_limit=self.settings.pairing_partner_limit,
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


def _pid(raw: dict) -> str:
    return str(raw.get("matched_product_id") or raw.get("product_id") or "")


def _require_known_product(raw: dict, verb: str) -> str:
    """An edit action can only target a product already in Grocy."""
    product_id = _pid(raw)
    if not product_id:
        name = str(raw.get("name") or "that").strip() or "that"
        raise ApiError(400, f"can't {verb} {name!r}: it isn't in your fridge")
    return product_id


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
