"""Tests for the path-resolution module.

Every public function in ``tailctl.paths`` honors ``$TAILCTL_HOME``; these tests
pin that behavior so a regression that silently writes to the real
``~/.tailctl`` during a test run is impossible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tailctl import paths


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TAILCTL_HOME", str(tmp_path))
    return tmp_path


def test_home_uses_env_override(tmp_home: Path) -> None:
    assert paths.home() == tmp_home


def test_home_defaults_to_user_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAILCTL_HOME", raising=False)
    assert paths.home() == Path("~/.tailctl").expanduser()


def test_all_paths_resolve_under_home(tmp_home: Path) -> None:
    for fn in (
        paths.profiles_yaml,
        paths.state_json,
        paths.state_lock,
        paths.log_jsonl,
        paths.log_jsonl_rotated,
        paths.fixtures_dir,
    ):
        assert fn().is_relative_to(tmp_home), f"{fn.__name__} escaped TAILCTL_HOME"


def test_state_lock_is_separate_file_from_state_json(tmp_home: Path) -> None:
    # The whole point of the separate lock file is that atomic rename of
    # state.json does not orphan the lock on the renamed inode.
    assert paths.state_lock() != paths.state_json()
    assert paths.state_lock().name == "state.json.lock"


def test_ensure_home_creates_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "deeply" / "nested"
    monkeypatch.setenv("TAILCTL_HOME", str(target))
    assert not target.exists()
    result = paths.ensure_home()
    assert result == target
    assert target.is_dir()
