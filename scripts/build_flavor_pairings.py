"""Generate a FoodBrain pairings bundle from real FlavorGraph node embeddings.

This is a one-off, offline data-generation step. It reads the public FlavorGraph
artifacts -- the node list (``nodes_191120.csv``, mapping node ids to ingredient
names and node types) and the trained 300D node embeddings pickle -- and writes
the queryable ``{"pairs": [...]}`` bundle that ``foodbrain_assistant.pairing``
consumes at runtime. The package runtime stays dependency-free; the heavy lifting
(numpy) lives here because this script runs offline and its output is a small,
plain JSON file.

Get the artifacts from the public FlavorGraph project (lamypark/FlavorGraph):

  * ``nodes_191120.csv`` -- in the repo under ``input/``::

        curl -sSL -o .foodbrain-local/flavorgraph/nodes_191120.csv \\
          https://raw.githubusercontent.com/lamypark/FlavorGraph/master/input/nodes_191120.csv

  * the 300D node-embedding pickle -- a ~10MB Google Drive download linked from
    the FlavorGraph README ("Pickle file containing the 300D FlavorGraph node
    embeddings"). Save it as
    ``.foodbrain-local/flavorgraph/node_embeddings.pickle``.

Security note: this loads a pickle, which can execute arbitrary code. Only run it
against the official FlavorGraph embeddings file you downloaded yourself.

The pickle is a ``{node_id (str): numpy.float32 array (300,)}`` dict. We keep only
node ids whose CSV ``node_type`` is ``ingredient`` (dropping flavor-compound
nodes), compute each ingredient's top-k neighbors by cosine similarity, and emit
undirected pairs with a 0..1 score. Output (default
``.foodbrain-local/pairings.json``) is intentionally local and ignored by git --
do not commit the embeddings or the generated bundle (both are data, not code).

Usage:
    PYTHONPATH=src python3 scripts/build_flavor_pairings.py
    PYTHONPATH=src python3 scripts/build_flavor_pairings.py \\
        --nodes-csv .foodbrain-local/flavorgraph/nodes_191120.csv \\
        --embeddings .foodbrain-local/flavorgraph/node_embeddings.pickle \\
        --out .foodbrain-local/pairings.json --top-k 10 --min-score 0.5

Then point the CLI at the bundle:
    PYTHONPATH=src python3 -m foodbrain_assistant.cli --sample \\
        --pairings-json .foodbrain-local/pairings.json
"""

import argparse
import csv
import json
import pickle
import sys
from pathlib import Path
from typing import Dict, List, Tuple

_DEFAULT_DIR = Path(".foodbrain-local")
_DEFAULT_NODES = _DEFAULT_DIR / "flavorgraph" / "nodes_191120.csv"
_DEFAULT_EMBEDDINGS = _DEFAULT_DIR / "flavorgraph" / "node_embeddings.pickle"
_DEFAULT_OUT = _DEFAULT_DIR / "pairings.json"


def main(argv: List[str]) -> int:
    args = _parse_args(argv)

    try:
        import numpy as np
    except ImportError:
        print(
            "numpy is required to build the pairings bundle "
            "(pip install numpy). It is only needed for this offline script.",
            file=sys.stderr,
        )
        return 2

    if not args.nodes_csv.exists():
        print(f"Node list not found: {args.nodes_csv}", file=sys.stderr)
        print(__doc__.split("Usage:")[0].strip(), file=sys.stderr)
        return 2
    if not args.embeddings.exists():
        print(f"Embeddings pickle not found: {args.embeddings}", file=sys.stderr)
        print(__doc__.split("Usage:")[0].strip(), file=sys.stderr)
        return 2

    id_to_name = _load_ingredient_names(args.nodes_csv)
    print(f"Loaded {len(id_to_name)} ingredient nodes from {args.nodes_csv}.")

    with args.embeddings.open("rb") as handle:
        embeddings = pickle.load(handle)
    if not isinstance(embeddings, dict):
        print(
            f"Unexpected embeddings format: {type(embeddings).__name__} "
            "(expected a {node_id: vector} dict).",
            file=sys.stderr,
        )
        return 1
    print(f"Loaded {len(embeddings)} node embeddings from {args.embeddings}.")

    # Keep only ingredient nodes that have an embedding, in a stable order so the
    # generated bundle is deterministic.
    node_ids = [nid for nid in sorted(id_to_name, key=_sort_key) if nid in embeddings]
    if not node_ids:
        print(
            "No ingredient nodes had embeddings -- do the CSV node ids match the "
            "pickle keys?",
            file=sys.stderr,
        )
        return 1
    names = [id_to_name[nid] for nid in node_ids]

    matrix = np.asarray([embeddings[nid] for nid in node_ids], dtype=np.float32)
    pairs = _top_pairs(np, matrix, names, args.top_k, args.min_score)
    print(
        f"Computed {len(pairs)} undirected pairs from {len(node_ids)} "
        f"ingredients (top-{args.top_k}, min score {args.min_score})."
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"pairs": pairs}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Wrote pairings bundle to {args.out}.")
    return 0


def _load_ingredient_names(path: Path) -> Dict[str, str]:
    """Map node id -> human-readable ingredient name for ingredient nodes only."""
    names: Dict[str, str] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if (row.get("node_type") or "").strip().lower() != "ingredient":
                continue
            node_id = (row.get("node_id") or "").strip()
            raw_name = (row.get("name") or "").strip()
            if not node_id or not raw_name:
                continue
            names[node_id] = _clean_name(raw_name)
    return names


def _clean_name(raw: str) -> str:
    """FlavorGraph names are underscore-joined, e.g. ``black_pepper``."""
    return raw.replace("_", " ").strip()


def _top_pairs(
    np,
    matrix,
    names: List[str],
    top_k: int,
    min_score: float,
) -> List[Dict[str, object]]:
    """Top-k cosine neighbors per row, deduped to undirected pairs (max score)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    unit = matrix / np.clip(norms, 1e-12, None)

    count = unit.shape[0]
    # Block the similarity computation so peak memory stays a few hundred MB even
    # for the full ~6.6k-ingredient graph.
    best: Dict[Tuple[str, str], float] = {}
    block = 512
    for start in range(0, count, block):
        stop = min(start + block, count)
        sims = unit[start:stop] @ unit.T  # (rows, count)
        for local_row, global_row in enumerate(range(start, stop)):
            row = sims[local_row]
            row[global_row] = -2.0  # exclude self
            # argpartition gives the top_k indices cheaply, then sort those.
            k = min(top_k, count - 1)
            top_idx = np.argpartition(-row, k - 1)[:k]
            top_idx = top_idx[np.argsort(-row[top_idx])]
            for neighbor in top_idx:
                score = float(row[neighbor])
                score = max(0.0, min(1.0, score))
                if score < min_score:
                    continue
                a, b = names[global_row], names[int(neighbor)]
                if a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                if key not in best or score > best[key]:
                    best[key] = score

    pairs = [
        {"a": a, "b": b, "score": round(score, 4)}
        for (a, b), score in best.items()
    ]
    pairs.sort(key=lambda p: (-p["score"], p["a"], p["b"]))
    return pairs


def _sort_key(node_id: str) -> Tuple[int, object]:
    """Sort numeric node ids numerically, falling back to string order."""
    return (0, int(node_id)) if node_id.isdigit() else (1, node_id)


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a FoodBrain pairings bundle from FlavorGraph embeddings.",
    )
    parser.add_argument("--nodes-csv", type=Path, default=_DEFAULT_NODES)
    parser.add_argument("--embeddings", type=Path, default=_DEFAULT_EMBEDDINGS)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Neighbors kept per ingredient (default 10).",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.0,
        help="Drop pairs whose cosine score is below this (0..1, default 0.0).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
