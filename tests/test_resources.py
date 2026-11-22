"""Text/data resources: every language file is complete and the code reads exactly what is in them."""
import json
from pathlib import Path

from app.core import resources
from app.core.i18n import LANGS, STATUS_LABELS, forecast_label, updated_ago

ROOT = Path(__file__).resolve().parents[1]


def _flat_langs(node, path=""):
    """Yield (path, dict) for every {en,de,fr,it} leaf in a resource tree."""
    if isinstance(node, dict):
        if set(node) >= {"en", "de"}:
            yield path, node
        else:
            for k, v in node.items():
                yield from _flat_langs(v, f"{path}/{k}")


def test_backend_resources_have_all_four_languages():
    for f in (ROOT / "app" / "resources").glob("*.json"):
        for path, leaf in _flat_langs(json.loads(f.read_text(encoding="utf-8"))):
            missing = [l for l in LANGS if not leaf.get(l)]
            assert not missing, f"{f.name}{path} lacks {missing}"


def test_frontend_language_files_have_identical_keys():
    d = {l: json.loads((ROOT / "static" / "i18n" / f"{l}.json").read_text(encoding="utf-8")) for l in LANGS}
    for l in LANGS:
        assert set(d[l]) == set(d["en"]), f"{l}.json differs from en.json: {set(d['en']) ^ set(d[l])}"
        assert all(isinstance(v, str) and v for v in d[l].values())


def test_code_uses_the_resource_files():
    assert STATUS_LABELS == resources.load_resource("i18n")["STATUS_LABELS"]
    assert "{" not in forecast_label("UNKNOWN", None, None, None)["de"]
    assert updated_ago(None, "de") == ""


def test_missing_resource_gives_a_clear_error():
    import pytest
    with pytest.raises(RuntimeError, match="Missing resource file"):
        resources.load_resource("does_not_exist")
