"""Dependency-free HTTP transport for the FoodBrain JSON API + SPA (steps 2 & 4).

A thin ``http.server`` wrapper around :class:`foodbrain_assistant.api.FoodBrainAPI`.
The runtime stays dependency-free (stdlib only), in keeping with the project rule.
Step 4 adds static serving of the single-file SPA (``prototype/fridge-now.html``)
from ``/`` and ``/ui`` so it can be embedded as a Home Assistant webpage panel
(``panel_iframe``); the SPA already resolves the API to the same origin when
served. See the README "HA webpage panel (build order step 4)" subsection.

Routes::

    GET  /                           -> the SPA (same as /ui)
    GET  /ui                         -> the SPA
    GET  /api/health
    GET  /api/stock                  -> stock-with-scores (bands view)
    POST /api/connect                {"selection": [product_id, ...]}
    POST /api/build-prompt           {"selection": [product_id, ...],
                                      "preferences": {"cuisine", "style", "needs"}}
    GET  /api/product-entries?product_id=ID
    POST /api/consume                {"product_id": ID, "amount": 1}
    POST /api/toss                   {"product_id": ID, "amount": 1, "confirm": true}
    POST /api/set-due-date           {"stock_entry_id": ID, "best_before_date": "YYYY-MM-DD"}
    POST /api/undo                   {"transaction_id": ID}
    POST /api/intake/understand      {"transcript": "...", "answers": "..."}
    POST /api/intake/commit          {"items": [{name, matched_product_id, amount, ...}]}
    POST /api/recipes/ideas          {"mode", "preferences"?, "idea_model"?, "balance"?, "count"?}
    POST /api/recipes/recipe         {"idea": {...}, "mode", "recipe_model"?}
    POST /api/recipes/twist          {"dish", "transcript"? | "text"?}
    POST /api/recipes/revise         {"recipe": {...}, "transcript"? | "text"?, "mode"?}
    POST /api/recipes/cooked         {"dish"}
    POST /api/recipes/save           {"title", "guidance": [...], "buy"?, "twist"?}
    POST /api/recipes/cook-estimate  {"dish", "guidance"?, "buy"?, "mode"?}
    POST /api/recipes/cook-commit    {"dish", "items": [...]}
    POST /api/recipes/add-missing    {"name", "amount"?, "unit"?, "location"?, "used"?}
    POST /api/recipes/cook-adjust    {"session_id", "line_index", "new_amount"}
    POST /api/recipes/cook-undo      {"session_id"}  -> reverse a whole session
    GET  /api/recipes/cook-history   -> past cooking sessions (Verlauf)
    GET  /api/recipes/book           -> saved "Meine Rezepte"
    GET  /api/recipes/config         -> model choices + defaults for Settings
    GET  /api/icons                  -> per-item emoji overrides (name -> emoji)
    POST /api/set-icon               {"name": "...", "emoji": "🥛"}  ("" clears)

CORS is permissive so the SPA can be developed from a separate dev origin.
"""

from argparse import ArgumentParser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Optional, Sequence

from urllib.parse import parse_qs, urlparse

from .aliases import AliasError, load_aliases, merge_aliases
from .api import ApiError, FoodBrainAPI
from .config import Settings, load_settings
from .grocy_client import GrocyClient, GrocyClientError, parse_stock_response
from .models import StockItem
from .normalization import str_list as _str_list
from .pairing import PairingError, load_pairings
from .recipes import RecipesError, parse_recipes_response


def make_handler(api: FoodBrainAPI, ui_html: Optional[bytes] = None):
    """Build a request handler class bound to a configured :class:`FoodBrainAPI`.

    ``ui_html``, when provided, is the SPA bundle served at ``/`` and ``/ui``
    (build order step 4). When ``None`` those routes return 404, leaving a
    pure-API server.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "FoodBrain/0.1"

        def do_OPTIONS(self) -> None:  # CORS preflight
            self._send(204, None)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path
            try:
                if route in ("/", "/ui", "/ui/", "/index.html"):
                    if ui_html is None:
                        raise ApiError(
                            404,
                            "SPA is not served by this instance "
                            "(start with the prototype present or pass --ui-file)",
                        )
                    self._send_html(ui_html)
                elif route == "/api/health":
                    self._send(
                        200,
                        {
                            "ok": True,
                            "intake_enabled": api.intake_understander is not None
                            or getattr(api.settings, "intake_enabled", False),
                        },
                    )
                elif route == "/api/stock":
                    self._send(200, api.stock_with_scores())
                elif route == "/api/product-entries":
                    product_id = _single(parse_qs(parsed.query).get("product_id"))
                    if not product_id:
                        raise ApiError(400, "product_id query parameter is required")
                    self._send(200, api.product_entries(product_id))
                elif route == "/api/locations":
                    self._send(200, api.get_locations())
                elif route == "/api/recipes/cook-history":
                    self._send(200, api.cook_history())
                elif route == "/api/recipes/book":
                    self._send(200, api.recipe_book())
                elif route == "/api/recipes/config":
                    self._send(200, _recipes_config(api))
                elif route == "/api/icons":
                    self._send(200, api.get_icons())
                else:
                    raise ApiError(404, f"no route for GET {route}")
            except ApiError as exc:
                self._error(exc)

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            try:
                body = self._read_json()
                if route == "/api/connect":
                    self._send(200, api.connect(_selection(body)))
                elif route == "/api/build-prompt":
                    self._send(
                        200,
                        api.build_prompt(_selection(body), _preferences(body)),
                    )
                elif route == "/api/consume":
                    self._send(
                        200,
                        api.consume(_require(body, "product_id"), _amount(body)),
                    )
                elif route == "/api/toss":
                    self._send(
                        200,
                        api.toss(
                            _require(body, "product_id"),
                            _amount(body),
                            confirm=bool(body.get("confirm", False)),
                        ),
                    )
                elif route == "/api/set-due-date":
                    self._send(
                        200,
                        api.set_due_date(
                            _require(body, "stock_entry_id"),
                            _require(body, "best_before_date"),
                            product_id=str(body.get("product_id", "")),
                        ),
                    )
                elif route == "/api/set-location":
                    self._send(
                        200,
                        api.set_location(
                            _require(body, "stock_entry_id"),
                            _require(body, "location_id"),
                            product_id=str(body.get("product_id", "")),
                        ),
                    )
                elif route == "/api/set-name":
                    self._send(
                        200,
                        api.set_name(
                            _require(body, "product_id"),
                            _require(body, "name"),
                        ),
                    )
                elif route == "/api/set-amount":
                    try:
                        new_amount = float(_require(body, "new_amount"))
                    except (TypeError, ValueError) as exc:
                        raise ApiError(400, "'new_amount' must be a number") from exc
                    self._send(
                        200,
                        api.set_amount(_require(body, "product_id"), new_amount),
                    )
                elif route == "/api/undo":
                    self._send(200, api.undo(_require(body, "transaction_id")))
                elif route == "/api/intake/understand":
                    self._send(
                        200,
                        api.intake_understand(
                            _require(body, "transcript"),
                            answers=str(body.get("answers", "")),
                            mode=str(body.get("mode", "add")),
                        ),
                    )
                elif route == "/api/intake/commit":
                    self._send(200, api.intake_commit(_items(body)))
                elif route == "/api/recipes/ideas":
                    self._send(
                        200,
                        api.recipe_ideas(
                            mode=str(body.get("mode", "stock")),
                            preferences=_preferences(body),
                            idea_model=_opt_str(body.get("idea_model")),
                            balance=_opt_float(body.get("balance")),
                            count=int(body.get("count", 8) or 8),
                        ),
                    )
                elif route == "/api/recipes/chat":
                    self._send(
                        200,
                        api.recipe_chat(
                            {
                                "message": _require(body, "message"),
                                "history": body.get("history"),
                                "preferences": _preferences(body),
                                "idea_model": _opt_str(body.get("idea_model")),
                            }
                        ),
                    )
                elif route == "/api/recipes/recipe":
                    self._send(
                        200,
                        api.recipe_detail(
                            _idea(body),
                            mode=str(body.get("mode", "stock")),
                            recipe_model=_opt_str(body.get("recipe_model")),
                        ),
                    )
                elif route == "/api/recipes/twist":
                    self._send(
                        200,
                        api.recipe_twist(
                            _require(body, "dish"),
                            transcript=str(body.get("transcript", "")),
                            text=str(body.get("text", "")),
                        ),
                    )
                elif route == "/api/recipes/revise":
                    self._send(
                        200,
                        api.recipe_revise(
                            _idea(body, key="recipe"),
                            transcript=str(body.get("transcript", "")),
                            text=str(body.get("text", "")),
                            mode=str(body.get("mode", "stock")),
                        ),
                    )
                elif route == "/api/recipes/cooked":
                    self._send(200, api.recipe_cooked(_require(body, "dish")))
                elif route == "/api/recipes/cook-estimate":
                    self._send(
                        200,
                        api.recipe_cook_estimate(
                            _require(body, "dish"),
                            guidance=_str_list(body.get("guidance")),
                            buy=_str_list(body.get("buy")),
                            mode=str(body.get("mode", "stock")),
                            correction=str(body.get("correction", "")),
                        ),
                    )
                elif route == "/api/recipes/cook-commit":
                    self._send(
                        200,
                        api.recipe_cook_commit(_require(body, "dish"), _items(body)),
                    )
                elif route == "/api/recipes/add-missing":
                    self._send(
                        200,
                        api.recipe_add_missing(
                            _require(body, "name"),
                            amount=body.get("amount", 1.0),
                            unit=(str(body.get("unit")) if body.get("unit") else None),
                            location=(
                                str(body.get("location"))
                                if body.get("location")
                                else None
                            ),
                            used=body.get("used", 0.0),
                        ),
                    )
                elif route == "/api/recipes/cook-undo":
                    self._send(
                        200, api.recipe_cook_undo(_require(body, "session_id"))
                    )
                elif route == "/api/recipes/cook-adjust":
                    try:
                        new_amount = float(_require(body, "new_amount"))
                    except (TypeError, ValueError) as exc:
                        raise ApiError(400, "'new_amount' must be a number") from exc
                    self._send(
                        200,
                        api.cook_adjust(
                            _require(body, "session_id"),
                            int(body.get("line_index", 0) or 0),
                            new_amount,
                        ),
                    )
                elif route == "/api/recipes/save":
                    self._send(
                        200,
                        api.recipe_save(
                            _require(body, "title"),
                            _str_list(body.get("guidance")),
                            buy=_str_list(body.get("buy")),
                            twist=str(body.get("twist", "")),
                        ),
                    )
                elif route == "/api/set-icon":
                    self._send(
                        200,
                        api.set_icon(
                            _require(body, "name"),
                            str(body.get("emoji", "")),
                        ),
                    )
                else:
                    raise ApiError(404, f"no route for POST {route}")
            except ApiError as exc:
                self._error(exc)

        # --- helpers ---

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ApiError(400, "request body was not valid JSON") from exc
            if not isinstance(parsed, dict):
                raise ApiError(400, "request body must be a JSON object")
            return parsed

        def _send_html(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            # No-cache so a deploy is picked up on the next load — otherwise
            # Safari/PWA serve a stale /ui and need a ?v=N bust each time.
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")  # HTTP/1.0 / old iOS Safari
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)

        def _send(self, status: int, payload: Optional[dict]) -> None:
            data = b"" if payload is None else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            if data:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _error(self, exc: ApiError) -> None:
            self._send(exc.status, {"error": exc.message})

        def log_message(self, fmt, *args) -> None:  # quieter default logging
            return

    return Handler


def _selection(body: dict) -> list:
    selection = body.get("selection")
    if not isinstance(selection, list):
        raise ApiError(400, "'selection' must be a list of product ids")
    return [str(value) for value in selection]


def _preferences(body: dict) -> dict:
    """The optional 'food mood' answers for the Ask-AI prompt builder."""
    prefs = body.get("preferences")
    return prefs if isinstance(prefs, dict) else {}


def _items(body: dict) -> list:
    items = body.get("items")
    if not isinstance(items, list):
        raise ApiError(400, "'items' must be a list of items to store")
    return items


def _idea(body: dict, key: str = "idea") -> dict:
    idea = body.get(key)
    if not isinstance(idea, dict):
        raise ApiError(400, f"{key!r} must be an object")
    return idea


def _opt_str(value):
    text = str(value or "").strip()
    return text or None


def _opt_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "'balance' must be a number") from exc


def _recipes_config(api: FoodBrainAPI) -> dict:
    """Curated model list + current defaults, for the SPA Settings panel."""
    from .recipes_llm import MODEL_CHOICES

    settings = api.settings
    return {
        "models": MODEL_CHOICES,
        "idea_model": getattr(settings, "idea_model", ""),
        "recipe_model": getattr(settings, "recipe_model", ""),
        "balance": getattr(settings, "recipe_explore_balance", 0.7),
        "enabled": getattr(settings, "intake_enabled", False),
    }


def _require(body: dict, key: str) -> str:
    value = body.get(key)
    if value in (None, ""):
        raise ApiError(400, f"'{key}' is required")
    return str(value)


def _amount(body: dict) -> float:
    try:
        return float(body.get("amount", 1.0))
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "'amount' must be a number") from exc


def _single(values) -> Optional[str]:
    if not values:
        return None
    return values[0]


# --- server bootstrap ---------------------------------------------------


def build_api(args, settings: Settings) -> FoodBrainAPI:
    stock_provider, source = _stock_provider(args, settings)
    return FoodBrainAPI(
        settings=settings,
        stock_provider=stock_provider,
        recipes=_load_recipes(args),
        pairings=_load_pairings(args),
        aliases=_load_aliases(args),
        write_client_factory=_write_client_factory(settings),
        product_catalog_provider=_catalog_provider(settings),
        source=source,
    )
    # idea_generator/recipe_generator/twist_extractor default to None so the
    # real recipes_llm.* run; cook_store_path defaults from settings.data_dir.


def _catalog_provider(settings: Settings):
    """Live product master list for intake matching (None outside live Grocy)."""
    if not settings.grocy_enabled:
        return None

    def catalog() -> list[dict]:
        client = GrocyClient(
            base_url=settings.grocy_base_url or "",
            api_key=settings.grocy_api_key or "",
        )
        return client.get_products()

    return catalog


def _stock_provider(args, settings: Settings):
    if args.sample:
        sample = _sample_stock()
        return (lambda: sample), "sample"
    if args.stock_json:
        items = parse_stock_response(_load_json(args.stock_json))
        return (lambda: items), "grocy-json"
    if not settings.grocy_enabled:
        raise SystemExit(
            "Set FOODBRAIN_GROCY_BASE_URL and FOODBRAIN_GROCY_API_KEY, "
            "or pass --sample / --stock-json PATH."
        )

    def live() -> list[StockItem]:
        client = GrocyClient(
            base_url=settings.grocy_base_url or "",
            api_key=settings.grocy_api_key or "",
        )
        return client.get_stock_items()

    return live, "grocy"


def _write_client_factory(settings: Settings):
    if not settings.grocy_enabled:
        return None

    def factory() -> GrocyClient:
        return GrocyClient(
            base_url=settings.grocy_base_url or "",
            api_key=settings.grocy_api_key or "",
            allow_writes=True,
        )

    return factory


def _sample_stock() -> list[StockItem]:
    today = date.today()
    return [
        StockItem("1", "Spinach", 1, "bag", today),
        StockItem("2", "Greek yogurt", 1, "tub", today + timedelta(days=2)),
        StockItem("3", "Carrots", 5, "pieces", today + timedelta(days=8)),
        StockItem("4", "Rice", 1, "kg", None),
    ]


def _load_recipes(args):
    if not args.recipes_json:
        return None
    try:
        return parse_recipes_response(_load_json(args.recipes_json))
    except RecipesError as exc:
        raise SystemExit(str(exc)) from exc


def _load_pairings(args):
    if not args.pairings_json:
        return None
    try:
        return load_pairings(_load_json(args.pairings_json))
    except PairingError as exc:
        raise SystemExit(str(exc)) from exc


def _load_aliases(args):
    try:
        if args.aliases_json:
            return load_aliases(_load_json(args.aliases_json))
        repo_root = Path(__file__).resolve().parents[2]
        sample = repo_root / "examples" / "aliases.sample.json"
        override = repo_root / ".foodbrain-local" / "aliases.json"
        aliases = None
        if sample.is_file():
            aliases = load_aliases(_load_json(sample))
        if override.is_file():
            aliases = merge_aliases(aliases or {}, load_aliases(_load_json(override)))
        return aliases
    except AliasError as exc:
        raise SystemExit(str(exc)) from exc


def _load_ui(args) -> Optional[bytes]:
    """Load the SPA bundle (build order step 4).

    Uses ``--ui-file PATH`` when given; otherwise auto-detects the in-repo
    ``prototype/fridge-now.html``. Returns ``None`` (pure-API server) only when
    neither is found and ``--ui-file`` was not requested.
    """
    path = args.ui_file
    if path is None:
        repo_root = Path(__file__).resolve().parents[2]
        candidate = repo_root / "prototype" / "fridge-now.html"
        path = candidate if candidate.is_file() else None
    if path is None:
        return None
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise SystemExit(f"Could not read UI file: {exc}") from exc


def _load_json(path: Path):
    try:
        with Path(path).open("r", encoding="utf-8") as file:
            return json.load(file)
    except OSError as exc:
        raise SystemExit(f"Could not read JSON file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"File was not valid JSON: {exc}") from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = ArgumentParser(description="Serve the FoodBrain JSON API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--sample", action="store_true", help="Serve built-in sample stock.")
    source.add_argument(
        "--stock-json", type=Path, metavar="PATH",
        help="Serve an exported Grocy /api/stock JSON file.",
    )
    parser.add_argument("--recipes-json", type=Path, metavar="PATH")
    parser.add_argument("--pairings-json", type=Path, metavar="PATH")
    parser.add_argument("--aliases-json", type=Path, metavar="PATH")
    parser.add_argument(
        "--ui-file", type=Path, metavar="PATH",
        help="Serve this SPA HTML at / and /ui (defaults to the in-repo prototype).",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    api = build_api(args, settings)
    ui_html = _load_ui(args)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(api, ui_html))
    base = f"http://{args.host}:{args.port}"
    print(f"FoodBrain API serving on {base} (source: {api.source})")
    if ui_html is not None:
        print(f"FoodBrain SPA serving on {base}/ui")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
