"""Loads the editable text/data resources in app/resources/*.json (wording, keywords, labels in EN/DE/FR/IT)."""
from __future__ import annotations

import json
from functools import lru_cache

from app.core.config import PROJECT_ROOT

RESOURCE_DIR = PROJECT_ROOT / "app" / "resources"


@lru_cache(maxsize=None)
def load_resource(name: str) -> dict:
    """Parsed ``app/resources/<name>.json`` (cached). Raises a clear error if the file is missing or invalid."""
    path = RESOURCE_DIR / f"{name}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing resource file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in resource file {path}: {exc}") from exc
