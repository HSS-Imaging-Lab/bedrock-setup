"""Shared fixtures: where the repo's files are."""

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The repository root."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def catalog_path(repo_root: Path) -> Path:
    """The Codex model catalog the launcher installs."""
    return repo_root / "templates" / "global-gpt6-models.json"


@pytest.fixture(scope="session")
def launcher_path(repo_root: Path) -> Path:
    """The bedrock-codex launcher script."""
    return repo_root / "scripts" / "bedrock-codex"
