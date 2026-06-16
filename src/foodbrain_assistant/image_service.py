"""On-demand food item icon generation with a disk cache.

Fallback chain:
  1. Disk cache  — instant, always tried first
  2. Local GPU   — FOODBRAIN_ICON_LOCAL_URL must point to the generation server
                   (HiDream-O1 or similar) running on the user's machine via Tailscale
  3. None        — UI falls back to emoji

The generation server is expected to accept:
  POST /generate  {"prompt": str, "size": int}  → PNG bytes
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional
from urllib import request as urllib_request
from urllib.error import URLError

log = logging.getLogger(__name__)

_PROMPT_TEMPLATE = (
    "A single {name} centered on a pure white background. "
    "Minimalist flat food illustration, one item only, clean and isolated. "
    "No text, no patterns, no multiple items, no decorative borders, no grid."
)


def get_icon(
    product_id: str,
    name: str,
    *,
    local_url: Optional[str],
    cache_dir: Path,
) -> Optional[bytes]:
    """Return PNG bytes for *name*, or None when no icon is available."""
    cache_path = _cache_path(cache_dir, product_id, name)

    if cache_path.exists():
        return cache_path.read_bytes()

    if not local_url:
        return None

    png = _generate(name, local_url)
    if png is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(png)
    return png


def bust_cache(product_id: str, name: str, *, cache_dir: Path) -> None:
    """Delete the cached icon for a product so the next request regenerates it."""
    p = _cache_path(cache_dir, product_id, name)
    if p.exists():
        p.unlink()


# --- internal ---


def _cache_path(cache_dir: Path, product_id: str, name: str) -> Path:
    key = hashlib.sha1(f"{product_id}:{name}".encode()).hexdigest()[:20]
    return cache_dir / f"{key}.png"


def _generate(name: str, local_url: str) -> Optional[bytes]:
    prompt = _PROMPT_TEMPLATE.format(name=name)
    payload = json.dumps({"prompt": prompt, "size": 128}).encode("utf-8")
    try:
        req = urllib_request.Request(
            f"{local_url}/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=120) as resp:
            if resp.status == 200:
                return resp.read()
            log.warning("icon server returned HTTP %d for %r", resp.status, name)
    except (URLError, OSError) as exc:
        log.warning("icon generation failed for %r: %s", name, exc)
    return None
