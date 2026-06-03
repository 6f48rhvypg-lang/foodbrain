"""FlavorGraph-style ingredient pairing suggestions (Phase 5).

FlavorGraph models ingredient affinity as a weighted graph derived from shared
flavor compounds and recipe co-occurrence. Its public artifact is a set of node
embeddings; the practical, queryable form of that data is a precomputed list of
top pairings per ingredient (the nearest neighbors of each embedding).

To keep the first implementation offline-first, deterministic, and
dependency-free like the rest of FoodBrain, pairings are loaded from a local
JSON file rather than computed from embeddings at runtime. The expected shape is
a list of undirected weighted pairs, or an object with a ``pairs`` list::

    {
      "pairs": [
        {"a": "tomato", "b": "basil", "score": 0.92},
        {"a": "spinach", "b": "garlic", "score": 0.81}
      ]
    }

Each pair is symmetric: listing ``tomato``/``basil`` makes ``basil`` a partner of
``tomato`` and vice versa. ``score`` is a 0..1 affinity (the cosine similarity of
the two FlavorGraph embeddings, if generated that way). The bundle can be
regenerated offline from real FlavorGraph embeddings and dropped in place without
code changes.

Suggestions are produced for the most urgent (soon-to-expire) stock ingredients,
so the feature answers "what goes with the thing I need to use up?" Partners that
are also in stock are flagged so they are immediately actionable. Lookup mirrors
the explainable token-containment heuristic used by ``matching`` so that a stock
item named "Greek yogurt" still resolves to the "yogurt" pairing node.
"""

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .models import FlavorPartner, FlavorSuggestion, IngredientUrgency, StockItem
from .normalization import normalize_ingredient_name


class PairingError(RuntimeError):
    pass


class PairingGraph:
    """Undirected weighted ingredient pairing graph keyed by normalized name."""

    def __init__(self, partners: Dict[str, List[Tuple[str, float]]]) -> None:
        # Each value is sorted by (-score, name) so suggestions are deterministic.
        self._partners = partners

    def __len__(self) -> int:
        return len(self._partners)

    def partners_for(
        self, name: str, aliases: Optional[Dict[str, str]] = None
    ) -> List[Tuple[str, float]]:
        """Return partners for a stock ingredient name.

        Tries an exact normalized match first, then the same token-containment
        fallback as recipe matching ("yogurt" resolves "greek yogurt"). An
        optional alias map resolves non-English names ("Milch" -> "milk") before
        lookup so live stock matches the English pairing nodes.
        """
        normalized = normalize_ingredient_name(name, aliases)
        if normalized in self._partners:
            return self._partners[normalized]

        item_tokens = _tokenize(name, aliases)
        if not item_tokens:
            return []
        for key, partners in self._partners.items():
            key_tokens = _tokenize(key)
            if _tokens_match(item_tokens, key_tokens):
                return partners
        return []


def load_pairings(payload: Any) -> PairingGraph:
    rows = _require_pair_rows(payload)

    # Keep the best score seen for each unordered pair and build a symmetric map.
    best: Dict[str, Dict[str, float]] = {}
    for index, row in enumerate(rows):
        a, b, score = _parse_pair(row, index)
        if a == b:
            continue
        _record(best, a, b, score)
        _record(best, b, a, score)

    partners: Dict[str, List[Tuple[str, float]]] = {}
    for name, neighbors in best.items():
        ranked = sorted(neighbors.items(), key=lambda item: (-item[1], item[0]))
        partners[name] = ranked
    return PairingGraph(partners)


def suggest_pairings(
    graph: PairingGraph,
    urgent_ingredients: Iterable[IngredientUrgency],
    stock_items: Iterable[StockItem],
    ingredient_limit: int = 5,
    partner_limit: int = 4,
    aliases: Optional[Dict[str, str]] = None,
) -> List[FlavorSuggestion]:
    stock_token_sets = [
        _tokenize(item.name, aliases) for item in stock_items if item.amount > 0
    ]

    suggestions: List[FlavorSuggestion] = []
    for urgency in urgent_ingredients:
        if len(suggestions) >= ingredient_limit:
            break
        partners = graph.partners_for(urgency.item.name, aliases)
        if not partners:
            continue
        flavor_partners = [
            FlavorPartner(
                name=partner_name,
                score=score,
                in_stock=_in_stock(partner_name, stock_token_sets),
            )
            for partner_name, score in partners[:partner_limit]
        ]
        suggestions.append(
            FlavorSuggestion(
                ingredient=urgency.item.name,
                urgency_score=urgency.urgency_score,
                partners=flavor_partners,
            )
        )
    return suggestions


def _record(best: Dict[str, Dict[str, float]], src: str, dst: str, score: float) -> None:
    neighbors = best.setdefault(src, {})
    if dst not in neighbors or score > neighbors[dst]:
        neighbors[dst] = score


def _require_pair_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and "pairs" in payload:
        payload = payload["pairs"]
    if not isinstance(payload, list):
        raise PairingError("Pairings file must be a list of pairs or {\"pairs\": [...]}")

    rows = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise PairingError(f"Pair {index} was not an object")
        rows.append(row)
    return rows


def _parse_pair(row: Dict[str, Any], index: int) -> Tuple[str, str, float]:
    a = normalize_ingredient_name(str(row.get("a") or ""))
    b = normalize_ingredient_name(str(row.get("b") or ""))
    if not a or not b:
        raise PairingError(f"Pair {index} is missing an 'a' or 'b' ingredient")
    score = _as_score(row.get("score"), index)
    return a, b, score


def _as_score(value: Any, index: int) -> float:
    if value is None:
        return 1.0
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise PairingError(f"Pair {index} has a non-numeric score") from exc
    if score < 0:
        raise PairingError(f"Pair {index} has a negative score")
    return score


def _in_stock(partner_name: str, stock_token_sets: List[Set[str]]) -> bool:
    partner_tokens = _tokenize(partner_name)
    if not partner_tokens:
        return False
    return any(_tokens_match(partner_tokens, tokens) for tokens in stock_token_sets)


# Token helpers mirror foodbrain_assistant.matching so pairing lookups resolve
# stock names the same way recipe matching does, while keeping this module
# self-contained.
def _tokens_match(left: Set[str], right: Set[str]) -> bool:
    if not left or not right:
        return False
    return left <= right or right <= left


def _tokenize(name: str, aliases: Optional[Dict[str, str]] = None) -> Set[str]:
    normalized = normalize_ingredient_name(name, aliases)
    return {_singularize(token) for token in normalized.split() if token}


def _singularize(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("oes", "ches", "shes", "sses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
