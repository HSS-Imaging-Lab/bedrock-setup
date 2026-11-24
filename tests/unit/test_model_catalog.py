"""Check the Codex model catalog and that the launcher's model IDs are in it."""

import json
import re
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

OPTIONAL_KEYS = {"multi_agent_reasoning_effort"}
SLUG_PREFIX = "global.openai."
MODEL_ID = re.compile(r"global\.openai\.[A-Za-z0-9._-]+")


@pytest.fixture(scope="module")
def models(catalog_path: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = json.loads(catalog_path.read_text(encoding="utf-8"))[
        "models"
    ]
    assert entries
    return entries


def test_slugs_are_unique(models: list[dict[str, object]]) -> None:
    slugs = [m["slug"] for m in models]
    assert len(slugs) == len(set(slugs))


def test_priorities_are_unique(models: list[dict[str, object]]) -> None:
    priorities = [m["priority"] for m in models]
    assert len(priorities) == len(set(priorities))


def test_entries_share_one_key_set(models: list[dict[str, object]]) -> None:
    required = [set(m) - OPTIONAL_KEYS for m in models]
    assert all(keys == required[0] for keys in required)
    assert all(set(m) - required[0] <= OPTIONAL_KEYS for m in models)


def test_default_reasoning_level_is_supported(models: list[dict[str, object]]) -> None:
    for model in models:
        supported = model["supported_reasoning_levels"]
        assert isinstance(supported, list)
        efforts = [level["effort"] for level in supported if isinstance(level, dict)]
        assert model["default_reasoning_level"] in efforts, model["slug"]


def test_every_slug_is_a_global_openai_profile(models: list[dict[str, object]]) -> None:
    for model in models:
        slug = model["slug"]
        assert isinstance(slug, str)
        assert slug.startswith(SLUG_PREFIX)


def test_launcher_model_ids_are_in_the_catalog(
    models: list[dict[str, object]], launcher_path: Path
) -> None:
    ids = set(MODEL_ID.findall(launcher_path.read_text(encoding="utf-8")))
    assert ids
    assert ids <= {m["slug"] for m in models}


def test_gpt_6_2_models_sort_first(models: list[dict[str, object]]) -> None:
    by_priority = sorted(models, key=lambda m: int(str(m["priority"])))
    assert [m["slug"] for m in by_priority[:2]] == [
        "global.openai.gpt-6.2-astra",
        "global.openai.gpt-6.2-sol",
    ]
