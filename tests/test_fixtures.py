"""Sanity check that all shipped fixtures parse as JSON and contain the
expected top-level keys. If Tailscale's CLI output shape ever drifts and
someone updates the fixtures incorrectly, this test fails loud before any
real test ever consumes the malformed input.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

SWITCH_LIST_FIXTURES = [
    "switch-list-multi.json",
    "switch-list-single.json",
    "switch-list-empty.json",
]

STATUS_FIXTURES = [
    "status-ready.json",
    "status-no-exit-node.json",
    "status-with-exit-node.json",
    "status-needslogin.json",
    "status-starting.json",
    "status-orphan-exit-node.json",
]


def test_all_fixture_files_exist(fixtures_path: Path) -> None:
    for name in SWITCH_LIST_FIXTURES + STATUS_FIXTURES:
        assert (fixtures_path / name).is_file(), f"missing fixture: {name}"


def test_switch_list_fixtures_are_arrays_of_account_records(
    load_fixture: Callable[[str], Any],
) -> None:
    for name in SWITCH_LIST_FIXTURES:
        data = load_fixture(name)
        assert isinstance(data, list), f"{name} is not a list"
        for entry in data:
            assert {"id", "tailnet", "account", "selected"}.issubset(
                entry.keys()
            ), f"{name} entry missing required keys: {entry}"


def test_status_fixtures_have_backend_state(
    load_fixture: Callable[[str], Any],
) -> None:
    for name in STATUS_FIXTURES:
        data = load_fixture(name)
        assert "BackendState" in data, f"{name} missing BackendState"


def test_switch_list_multi_has_exactly_one_selected(
    load_fixture: Callable[[str], Any],
) -> None:
    data = load_fixture("switch-list-multi.json")
    selected = [entry for entry in data if entry["selected"]]
    assert len(selected) == 1, f"expected exactly one selected, got {len(selected)}"


def test_status_with_exit_node_has_exit_node_status(
    load_fixture: Callable[[str], Any],
) -> None:
    data = load_fixture("status-with-exit-node.json")
    assert data["ExitNodeStatus"] is not None
    assert data["ExitNodeStatus"]["Online"] is True
