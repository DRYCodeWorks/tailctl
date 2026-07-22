"""Two-factor process liveness for tailctl holders and waiters.

A holder is alive iff its PID is still running AND that process's create-time
matches the value recorded when the holder was registered. The create-time
check is what defeats PID reuse: macOS recycles PIDs aggressively, so a bare
``os.kill(pid, 0)`` can wrongly report a dead session as alive.

The module is structured around a ``ProcessTable`` protocol so the production
code uses ``psutil`` and tests use ``FakeProcessTable`` — simulating PID reuse
in real OS processes is unreliable on macOS.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import psutil


class ProcessTable(Protocol):
    """Minimal interface needed to capture and verify a process identity.

    ``create_time`` is float seconds since epoch (locale-independent,
    sub-second resolution). ``is_alive`` does the (pid, create_time) two-factor
    check that PID reuse cannot fool.
    """

    def create_time(self, pid: int) -> float: ...

    def is_alive(self, pid: int, expected_create_time: float) -> bool: ...

    def pids_for_socket(self, socket_path: str) -> list[int]: ...


@dataclass(frozen=True)
class ProcessIdentity:
    """A captured (pid, create_time) pair.

    Pass the resulting identity into ``ProcessTable.is_alive`` to test
    whether the same process is still running.
    """

    pid: int
    create_time: float

    @classmethod
    def capture(cls, table: ProcessTable, pid: int) -> ProcessIdentity:
        return cls(pid=pid, create_time=table.create_time(pid))


class PsutilProcessTable:
    """Production implementation of ``ProcessTable`` backed by psutil."""

    def create_time(self, pid: int) -> float:
        return psutil.Process(pid).create_time()

    def is_alive(self, pid: int, expected_create_time: float) -> bool:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return False
        try:
            actual = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        # Allow a small tolerance since create_time is float, but PID reuse on
        # macOS happens on second-ish boundaries — a tight epsilon is enough.
        return abs(actual - expected_create_time) < 0.001

    def pids_for_socket(self, socket_path: str) -> list[int]:
        """Pids of running tailscaled daemons bound to ``socket_path``.

        Matched by an exact ``--socket=<path>`` argv token. Used to find a
        leaked daemon that still holds an instance's UDS but has no live
        registry row (spawn race, a row dropped without killing the pid, or
        create_time drift hiding it from the two-factor check) — it would
        otherwise wedge the next spawn with ``address already in use``.
        """
        needle = f"--socket={socket_path}"
        found: list[int] = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if needle in cmdline:
                found.append(proc.info["pid"])
        return found


class FakeProcessTable:
    """Test double for ``ProcessTable``.

    Construct with a dict mapping pid → create_time. Use ``kill(pid)`` to
    remove a process, and ``reuse(pid, new_create_time)`` to simulate a new
    process being assigned the same PID — the key case real OS APIs make
    hard to test deterministically.
    """

    def __init__(self, processes: dict[int, float] | None = None) -> None:
        self._procs: dict[int, float] = dict(processes or {})
        self._sockets: dict[int, str] = {}

    def create_time(self, pid: int) -> float:
        if pid not in self._procs:
            raise LookupError(f"pid {pid} not in fake process table")
        return self._procs[pid]

    def is_alive(self, pid: int, expected_create_time: float) -> bool:
        if pid not in self._procs:
            return False
        return abs(self._procs[pid] - expected_create_time) < 0.001

    def pids_for_socket(self, socket_path: str) -> list[int]:
        return [
            pid
            for pid, sock in self._sockets.items()
            if sock == socket_path and pid in self._procs
        ]

    # --- test helpers ---

    def kill(self, pid: int) -> None:
        self._procs.pop(pid, None)
        self._sockets.pop(pid, None)

    def attach_socket(self, pid: int, socket_path: str) -> None:
        """Record that ``pid`` is bound to ``socket_path`` (for orphan tests)."""
        self._sockets[pid] = socket_path

    def reuse(self, pid: int, new_create_time: float) -> None:
        """Simulate PID reuse: same pid, different create_time."""
        self._procs[pid] = new_create_time

    def spawn(self, pid: int, create_time: float) -> None:
        self._procs[pid] = create_time
