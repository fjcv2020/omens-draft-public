from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def card_color_from_id(card_id: object) -> str:
    value = _norm(card_id)
    for color in ("red", "yellow", "blue"):
        if value.endswith(f"_{color}"):
            return color
    return ""


def card_image_cache_key(name: object, card_id: object, color: object = "") -> str:
    source = "|".join([_norm(name), _norm(card_id), _norm(color)])
    return sha256(source.encode("utf-8")).hexdigest()


def _printing_image_url(card: dict[str, Any]) -> str:
    printings = card.get("printings")
    if not isinstance(printings, list):
        return ""
    for printing in printings:
        if isinstance(printing, dict) and printing.get("image_url"):
            return str(printing["image_url"])
    return ""


def select_goagain_image_url(
    payload: dict[str, Any],
    *,
    name: object,
    card_id: object,
    color: object = "",
) -> str:
    cards = payload.get("data")
    if not isinstance(cards, list):
        return ""
    wanted_name = _norm(name)
    wanted_color = _norm(color) or card_color_from_id(card_id)
    candidates = [card for card in cards if isinstance(card, dict) and _printing_image_url(card)]
    if not candidates:
        return ""
    named = [card for card in candidates if _norm(card.get("name")) == wanted_name] or candidates
    colored = [card for card in named if wanted_color and _norm(card.get("color")) == wanted_color]
    return _printing_image_url((colored or named)[0])


def guess_image_extension(url: object, content_type: object = "") -> str:
    path = urlparse(str(url or "")).path.lower()
    for ext in (".webp", ".png", ".jpg", ".jpeg"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    ctype = _norm(content_type)
    if "webp" in ctype:
        return ".webp"
    if "png" in ctype:
        return ".png"
    if "jpeg" in ctype or "jpg" in ctype:
        return ".jpg"
    return ".webp"


def safe_cached_image_path(cache_dir: str | Path, filename: str) -> Path | None:
    if not filename:
        return None
    if not all(ch.isalnum() or ch in {".", "-", "_"} for ch in filename):
        return None
    if Path(filename).name != filename:
        return None
    path = (Path(cache_dir) / filename).resolve()
    root = Path(cache_dir).resolve()
    if not path.is_relative_to(root):
        return None
    if path.suffix.lower() not in {".webp", ".png", ".jpg"}:
        return None
    return path
