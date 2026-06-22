"""Benchmark OpenRouter models on the real voice-intake path.

For every case in ``bench/cases.json`` and every model in MODELS, this calls the
production ``understand_transcript()`` + ``reconcile_items()``, scores the result
against the case's sparse ``expect`` block, and reports per-model accuracy, real
$ cost (from OpenRouter's usage.cost), and latency.

    python bench/run_benchmark.py                 # all models in MODELS
    python bench/run_benchmark.py google/gemini-2.5-flash-lite  # only these

Needs FOODBRAIN_OPENROUTER_API_KEY in .env (already set for the live app).
"""

import difflib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, "src")

from foodbrain_assistant.config import load_settings
from foodbrain_assistant.intake import (
    IntakeError,
    reconcile_items,
    understand_transcript,
)
from foodbrain_assistant.llm import http_post
from foodbrain_assistant.normalization import normalize_ingredient_name

# Models to compare. First is the current production baseline.
MODELS = [
    "google/gemini-3.5-flash",
    "google/gemini-3.1-flash-lite",
    "google/gemini-3.1-flash-lite-preview",
    "google/gemini-2.5-flash-lite",
]

# Fields scored when present in an expected item. freshness_days uses tolerance.
SCALAR_FIELDS = ["quantity", "unit", "opened", "location", "action"]
FRESHNESS_TOLERANCE_DAYS = 2

CASES_PATH = Path("bench/cases.json")


def _norm(value):
    return normalize_ingredient_name(str(value or ""))


def _names_match(target, candidate):
    """Lenient name match: tolerant of word order and German compounding.

    'marmelade himbeeren' should match 'himbeermarmelade'. We compare on
    space-stripped normalized strings via substring OR a difflib ratio, so
    faithful-but-reordered names are not scored as misses.
    """
    if not target or not candidate:
        return False
    a, b = target.replace(" ", ""), candidate.replace(" ", "")
    if a in b or b in a:
        return True
    # Longest shared run handles German compound reversal
    # ('marmeladehimbeeren' vs 'himbeermarmelade' share 'marmelade'=9).
    block = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    if block.size >= 6:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.72


def _match_item(expected_name, actual_items, used):
    """Find the unused actual item whose name (raw or matched) fits expected."""
    target = _norm(expected_name)
    for idx, item in enumerate(actual_items):
        if idx in used:
            continue
        names = [_norm(item.name), _norm(item.matched_product_name)]
        if any(_names_match(target, n) for n in names):
            return idx
    return None


def _field_ok(field, expected, item):
    actual = getattr(item, field)
    if field == "quantity":
        try:
            return abs(float(actual) - float(expected)) < 1e-6
        except (TypeError, ValueError):
            return False
    if field == "unit":
        return _norm(actual) == _norm(expected)
    return actual == expected


def score_case(case, result, reconciled):
    expect = case.get("expect", {})
    exp_items = expect.get("items", [])
    used = set()
    matched = 0
    field_total = 0
    field_hits = 0
    misses = []

    for exp in exp_items:
        idx = _match_item(exp["name"], reconciled, used)
        if idx is None:
            misses.append(f"missing item '{exp['name']}'")
            # count its fields as failures so silence is penalised
            field_total += sum(1 for k in exp if k != "name")
            continue
        used.add(idx)
        matched += 1
        item = reconciled[idx]
        for field, val in exp.items():
            if field == "name":
                continue
            if field == "freshness_days":
                field_total += 1
                ok = item.freshness_days is not None and abs(
                    item.freshness_days - val
                ) <= FRESHNESS_TOLERANCE_DAYS
            else:
                field_total += 1
                ok = _field_ok(field, val, item)
            field_hits += 1 if ok else 0
            if not ok:
                misses.append(f"{exp['name']}.{field}={getattr(item, field, None)!r} (want {val!r})")

    extra = len(reconciled) - matched
    if extra > 0:
        misses.append(f"{extra} extra/hallucinated item(s)")

    recall = matched / len(exp_items) if exp_items else 1.0
    precision = matched / len(reconciled) if reconciled else (1.0 if not exp_items else 0.0)
    item_f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    field_acc = (field_hits / field_total) if field_total else 1.0

    max_q = expect.get("max_questions")
    q_ok = True if max_q is None else len(result.questions) <= max_q

    score = item_f1 * field_acc * (1.0 if q_ok else 0.9)
    return {
        "score": score,
        "item_f1": item_f1,
        "field_acc": field_acc,
        "q_ok": q_ok,
        "misses": misses,
    }


def run_model(model, cases, default_catalog, base_settings):
    settings = replace(base_settings, openrouter_model=model)
    captured = {}

    def transport(url, headers, body, timeout):
        raw = http_post(url, headers, body, timeout)
        captured["raw"] = raw
        return raw

    total_score = 0.0
    total_cost = 0.0
    total_latency = 0.0
    n = 0
    print(f"\n=== {model} ===")
    for case in cases:
        names = case.get("catalog", default_catalog)
        catalog = [{"id": str(i), "name": nm} for i, nm in enumerate(names)]
        t0 = time.time()
        try:
            result = understand_transcript(
                case["transcript"],
                settings=settings,
                catalog=catalog,
                answers=case.get("answers"),
                mode=case.get("mode", "add"),
                transport=transport,
            )
        except IntakeError as exc:
            print(f"  [{case['id']}] ERROR: {exc}")
            n += 1
            continue
        latency = time.time() - t0
        reconciled = reconcile_items(result.items, catalog)
        sc = score_case(case, result, reconciled)
        usage = json.loads(captured.get("raw", "{}")).get("usage", {})
        cost = float(usage.get("cost") or 0.0)
        total_score += sc["score"]
        total_cost += cost
        total_latency += latency
        n += 1
        flag = "OK " if sc["score"] >= 0.999 else "!! "
        print(
            f"  {flag}[{case['id']}] score={sc['score']:.2f} "
            f"f1={sc['item_f1']:.2f} fields={sc['field_acc']:.2f} "
            f"cost=${cost:.5f} {latency:.1f}s"
        )
        for miss in sc["misses"]:
            print(f"        - {miss}")
    avg_score = total_score / n if n else 0.0
    return {
        "model": model,
        "avg_score": avg_score,
        "total_cost": total_cost,
        "avg_cost": total_cost / n if n else 0.0,
        "avg_latency": total_latency / n if n else 0.0,
        "n": n,
    }


def main():
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    default_catalog = data.get("default_catalog", [])
    models = sys.argv[1:] or MODELS

    base_settings = load_settings()
    if not base_settings.openrouter_api_key:
        sys.exit("FOODBRAIN_OPENROUTER_API_KEY is not set")

    summaries = [run_model(m, cases, default_catalog, base_settings) for m in models]

    print("\n" + "=" * 72)
    print(f"{'model':40} {'score':>6} {'$/case':>9} {'$/1000':>9} {'lat':>6}")
    print("-" * 72)
    for s in sorted(summaries, key=lambda x: (-x["avg_score"], x["avg_cost"])):
        print(
            f"{s['model']:40} {s['avg_score']:>6.2f} "
            f"{s['avg_cost']:>9.5f} {s['avg_cost']*1000:>9.2f} {s['avg_latency']:>5.1f}s"
        )
    print("=" * 72)
    print("score 1.00 = perfect on every expected field; $/1000 = projected cost per 1000 intakes")


if __name__ == "__main__":
    main()
