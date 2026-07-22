"""Filesystem path resolution for tailctl runtime state.

All on-disk state lives under a single root directory, defaulting to
``~/.tailctl`` and overridable via the ``TAILCTL_HOME`` environment variable.
The override is what makes tests hermetic: a test sets ``TAILCTL_HOME`` to a
tmp dir and every path-returning function in this module follows it.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR = "TAILCTL_HOME"
_DEFAULT_HOME = "~/.tailctl"


def home() -> Path:
    """Return the tailctl state root directory.

    Honors ``$TAILCTL_HOME`` if set, falling back to ``~/.tailctl``.
    """
    return Path(os.environ.get(_ENV_VAR, _DEFAULT_HOME)).expanduser()


def profiles_yaml() -> Path:
    return home() / "profiles.yaml"


def state_json() -> Path:
    return home() / "state.json"


def state_lock() -> Path:
    """Separate lock file; never atomic-renamed.

    The flock is held on this file's inode so that an atomic rename of
    ``state.json`` itself does not orphan the lock.
    """
    return home() / "state.json.lock"


def instances_json() -> Path:
    """Registry of running per-profile userspace tailscaled instances."""
    return home() / "instances.json"


def instances_lock() -> Path:
    """Separate lock file for the instances registry (never atomic-renamed)."""
    return home() / "instances.json.lock"


def instances_dir() -> Path:
    """Root for per-instance runtime dirs (socket + statedir + logs)."""
    return home() / "instances"


def instance_dir(profile: str) -> Path:
    """Per-profile runtime dir holding its tailscaled socket, statedir, logs."""
    return instances_dir() / profile


def log_jsonl() -> Path:
    return home() / "log.jsonl"


def log_jsonl_rotated() -> Path:
    return home() / "log.1.jsonl"


def fixtures_dir() -> Path:
    return home() / "fixtures"


def ensure_home() -> Path:
    """Create the state root if it doesn't exist; return its path."""
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    return h
