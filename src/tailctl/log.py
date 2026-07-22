"""Append-only JSON log with a single-generation 10 MB rotation.

Every coordinator action (acquire, release, switch, reconciliation, reap,
rollback escalation) writes one JSON object per line. Format is intentionally
simple: a single object, no nesting beyond the immediate keys.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tailctl import paths

ROTATE_AT_BYTES = 10 * 1024 * 1024  # 10 MB


def append(record: dict[str, Any], *, log_path: Path | None = None) -> None:
    """Append one record. Rotates if the file would exceed ``ROTATE_AT_BYTES``."""
    target = log_path or paths.log_jsonl()
    target.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(target)
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(line)


def _rotate_if_needed(target: Path) -> None:
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return
    if size < ROTATE_AT_BYTES:
        return
    rotated = paths.log_jsonl_rotated() if target == paths.log_jsonl() else target.with_suffix(
        target.suffix + ".1"
    )
    # Overwrite any prior rotation generation; we keep exactly one.
    if rotated.exists():
        rotated.unlink()
    os.replace(target, rotated)
