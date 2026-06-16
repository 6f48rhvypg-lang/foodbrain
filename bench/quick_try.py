"""Throwaway: fire one German transcript at a chosen OpenRouter model.

Usage: python bench/quick_try.py [model-slug]
Defaults to the Gemini 3.1 Flash-Lite preview we want to evaluate.
"""

import json
import sys
from dataclasses import replace

sys.path.insert(0, "src")

from foodbrain_assistant.config import load_settings
from foodbrain_assistant.intake import understand_transcript, reconcile_items, _http_post

MODEL = sys.argv[1] if len(sys.argv) > 1 else "google/gemini-3.1-flash-lite-preview"

# A messy, realistic German "talking at the fridge" transcript.
TRANSCRIPT = (
    "also ich hab hier noch ne angebrochene packung butter, "
    "zwei liter milch, die ist noch ungefähr ne woche gut, "
    "und drei joghurt, glaub griechischer, und ähm karotten so ein halbes kilo"
)

CATALOG = [
    {"id": "1", "name": "Butter"},
    {"id": "2", "name": "Milch"},
    {"id": "3", "name": "Griechischer Joghurt"},
    {"id": "4", "name": "Karotten"},
]

settings = replace(load_settings(), openrouter_model=MODEL)

captured = {}


def transport(url, headers, body, timeout):
    raw = _http_post(url, headers, body, timeout)
    captured["raw"] = raw
    return raw


print(f"Model: {MODEL}\nTranscript: {TRANSCRIPT}\n")
result = understand_transcript(
    TRANSCRIPT, settings=settings, catalog=CATALOG, transport=transport
)
reconciled = reconcile_items(result.items, CATALOG)

for item in reconciled:
    print(
        f"  - {item.name!r} qty={item.quantity} unit={item.unit} "
        f"opened={item.opened} fresh={item.freshness_days}d "
        f"loc={item.location} match={item.match}->{item.matched_product_name}"
    )
print(f"\nQuestions: {result.questions}")
print(f"Summary:   {result.summary}")

usage = json.loads(captured["raw"]).get("usage", {})
print(f"\nUsage: {usage}")
