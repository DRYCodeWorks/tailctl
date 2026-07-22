"""On-disk registry of running userspace tailscaled instances.

Replaces the global ``state.json`` coordination model. There is no shared
network to coordinate anymore: each profile that a session needs gets its own
userspace ``tailscaled`` (own socket, statedir, SOCKS/HTTP proxy ports), and
several run simultaneously. This registry is a lightweight table — NOT a state
machine — recording which profiles have a live instance, the ports they expose,
their port-forwards, and a refcount so multiple sessions can share one instance.

The locking discipline mirrors ``state.py``: an exclusive ``fcntl.flock`` on a
SEPARATE ``instances.json.lock`` file, with the registry itself read/mutated/
written atomically via ``tmp + os.replace``.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tailctl import paths

REGISTRY_SCHEMA_VERSION = 1


@dataclass
class Forward:
    """One active port-forward for an instance."""

    service: str
    local_port: int
    remote_host: str
    remote_port: int
    pid: int | None = None  # relay subprocess pid (None until started)
    create_time: float | None = None


@dataclass
class Holder:
    """A live claim on an instance by some owning process.

    The refcount is ``len(holders)``; each holder is identified by a two-factor
    ``(owner_pid, owner_create_time)`` so a crashed owner's claim can be reaped
    (and PID reuse can't resurrect it). ``up`` adds a holder; ``down`` removes
    the caller's; ``reap`` drops dead-owner holders and stops orphaned daemons.
    """

    owner_pid: int
    owner_create_time: float
    since: str = ""


@dataclass
class Instance:
    """A running per-profile userspace tailscaled, plus its forwards/holders."""

    profile: str
    pid: int
    create_time: float
    socket: str
    statedir: str
    socks_port: int
    http_port: int
    created_at: str = ""
    forwards: list[Forward] = field(default_factory=list)
    holders: list[Holder] = field(default_factory=list)
    # False while the daemon is registered but awaiting first login; flips True
    # once exit node + forwards are applied. Distinguishes a pending instance
    # (which already has a holder) from a finalized one.
    finalized: bool = True
    # Epoch seconds when the last holder released gracefully — starts the linger
    # window during which the warm daemon is kept for fast reuse. None while held
    # (or when the owner crashed: then reap stops it immediately, no grace).
    released_at: float | None = None

    @property
    def refcount(self) -> int:
        return len(self.holders)


@dataclass
class Registry:
    schema_version: int = REGISTRY_SCHEMA_VERSION
    generation: int = 0
    instances: dict[str, Instance] = field(default_factory=dict)


class RegistryCorruptError(RuntimeError):
    """instances.json could not be parsed."""


def _registry_from_dict(data: dict[str, Any]) -> Registry:
    if data.get("schema_version") not in (None, REGISTRY_SCHEMA_VERSION):
        raise ValueError(
            f"unrecognized registry schema_version: {data.get('schema_version')!r}; "
            f"expected {REGISTRY_SCHEMA_VERSION}"
        )
    instances: dict[str, Instance] = {}
    _fields = (
        "profile", "pid", "create_time", "socket", "statedir",
        "socks_port", "http_port", "created_at", "finalized", "released_at",
    )
    for name, body in (data.get("instances") or {}).items():
        forwards = [Forward(**f) for f in body.get("forwards", [])]
        holders = [Holder(**h) for h in body.get("holders", [])]
        # Only pass known scalar fields; drop anything else (e.g. a legacy
        # ``refcount`` key, now a derived property).
        known = {k: body[k] for k in _fields if k in body}
        instances[name] = Instance(forwards=forwards, holders=holders, **known)
    return Registry(
        schema_version=REGISTRY_SCHEMA_VERSION,
        generation=int(data.get("generation", 0)),
        instances=instances,
    )


class RegistryStore:
    """Atomic, flock-coordinated access to instances.json.

    Same contract as ``state.StateStore``: ``transaction()`` yields a mutable
    ``Registry`` written on clean exit; ``read()`` is a lock-free snapshot.
    """

    def __init__(
        self, *, registry_path: Path | None = None, lock_path: Path | None = None
    ) -> None:
        self._path = registry_path or paths.instances_json()
        self._lock_path = lock_path or paths.instances_lock()

    @contextmanager
    def transaction(self) -> Iterator[Registry]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            reg = self._read_unlocked()
            yield reg
            reg.generation += 1
            self._write_unlocked(reg)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def read(self) -> Registry:
        return self._read_unlocked()

    def _read_unlocked(self) -> Registry:
        if not self._path.exists():
            return Registry()
        try:
            data = json.loads(self._path.read_text())
        except json.JSONDecodeError as exc:
            raise RegistryCorruptError(
                f"registry at {self._path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise RegistryCorruptError(f"registry at {self._path} is not a JSON object")
        return _registry_from_dict(data)

    def _write_unlocked(self, reg: Registry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = json.dumps(
            {
                "schema_version": reg.schema_version,
                "generation": reg.generation,
                "instances": {n: asdict(i) for n, i in reg.instances.items()},
            },
            indent=2,
            sort_keys=True,
        )
        tmp.write_text(payload)
        os.replace(tmp, self._path)
