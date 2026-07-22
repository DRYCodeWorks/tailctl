"""Shared pytest fixtures.

``fixtures_path`` exposes the on-disk fixtures dir and ``load_fixture`` reads
one as parsed JSON. Subsequent test modules use these to feed deterministic
inputs to the tailscale CLI parser.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_path() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    def _load(name: str) -> Any:
        return json.loads((FIXTURES_DIR / name).read_text())

    return _load
