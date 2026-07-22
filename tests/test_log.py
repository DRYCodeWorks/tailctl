"""Log append + single-generation rotation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tailctl import paths
from tailctl.log import ROTATE_AT_BYTES, append


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TAILCTL_HOME", str(tmp_path))
    return tmp_path


def test_append_writes_jsonl(home: Path) -> None:
    append({"action": "acquire", "session_id": "s1"})
    append({"action": "release", "session_id": "s1"})
    lines = paths.log_jsonl().read_text().strip().splitlines()
    assert len(lines) == 2
    import json
    assert json.loads(lines[0])["action"] == "acquire"
    assert json.loads(lines[1])["action"] == "release"


def test_no_rotation_below_threshold(home: Path) -> None:
    for i in range(100):
        append({"i": i})
    assert paths.log_jsonl().exists()
    assert not paths.log_jsonl_rotated().exists()


def test_rotation_when_threshold_exceeded(home: Path) -> None:
    # Write a single large entry to exceed the threshold cheaply.
    big = "x" * (ROTATE_AT_BYTES + 100)
    append({"blob": big})  # writes ~10 MB+
    # Next append should trigger a rotate before write.
    append({"action": "after-rotate"})
    assert paths.log_jsonl_rotated().exists()
    # The new active log contains only the post-rotation entry.
    active = paths.log_jsonl().read_text().strip().splitlines()
    assert len(active) == 1
    import json
    assert json.loads(active[0])["action"] == "after-rotate"


def test_rotation_overwrites_existing_rotated(home: Path) -> None:
    """We keep exactly one rotation generation. Calling rotate twice should
    overwrite the prior .1 file, not stack a .2.
    """
    big = "x" * (ROTATE_AT_BYTES + 100)
    append({"gen": 1, "blob": big})
    append({"action": "after-first-rotate"})
    assert paths.log_jsonl_rotated().exists()
    first_rotation = paths.log_jsonl_rotated().read_text()

    # Force another rotation.
    append({"gen": 2, "blob": big})
    append({"action": "after-second-rotate"})
    # Rotated file is overwritten with the contents from just before the
    # second rotation, NOT preserved from the first.
    second_rotation = paths.log_jsonl_rotated().read_text()
    assert second_rotation != first_rotation
    # No log.2.jsonl exists; we only keep one rotation generation.
    assert not (home / "log.2.jsonl").exists()
