"""Liveness checks must defeat PID reuse.

The production ``PsutilProcessTable`` is exercised against the calling pytest
process (a real, alive process). PID reuse is simulated via ``FakeProcessTable``
because forcing real PID reuse on macOS is not reliably reproducible.
"""

from __future__ import annotations

import os

import psutil
import pytest

from tailctl.liveness import (
    FakeProcessTable,
    ProcessIdentity,
    PsutilProcessTable,
)

# --- ProcessIdentity ---


def test_capture_records_pid_and_create_time() -> None:
    table = FakeProcessTable({42: 1000.0})
    identity = ProcessIdentity.capture(table, 42)
    assert identity.pid == 42
    assert identity.create_time == 1000.0


# --- PsutilProcessTable: real-process happy path ---


def test_psutil_current_process_is_alive() -> None:
    table = PsutilProcessTable()
    pid = os.getpid()
    ct = table.create_time(pid)
    assert table.is_alive(pid, ct)


def test_psutil_create_time_matches_psutil_directly() -> None:
    table = PsutilProcessTable()
    pid = os.getpid()
    assert table.create_time(pid) == psutil.Process(pid).create_time()


def test_psutil_nonexistent_pid_is_not_alive() -> None:
    table = PsutilProcessTable()
    # A pid that is extremely unlikely to be a live process.
    dead_pid = 999_999_999
    assert not table.is_alive(dead_pid, 1.0)


def test_psutil_wrong_create_time_is_not_alive() -> None:
    table = PsutilProcessTable()
    pid = os.getpid()
    real_ct = table.create_time(pid)
    # Same PID, wrong recorded create_time → PID reuse scenario → not alive.
    assert not table.is_alive(pid, real_ct + 100.0)


# --- FakeProcessTable: PID reuse simulation (the key case) ---


def test_fake_alive_process() -> None:
    table = FakeProcessTable({100: 5000.0})
    assert table.is_alive(100, 5000.0)


def test_fake_killed_process_is_not_alive() -> None:
    table = FakeProcessTable({100: 5000.0})
    table.kill(100)
    assert not table.is_alive(100, 5000.0)


def test_fake_pid_reuse_defeats_is_alive() -> None:
    # The headline case. Holder recorded (pid=100, create_time=5000.0).
    # That process dies; OS reuses pid=100 for an unrelated process with
    # create_time=8000.0. The reaper must NOT consider the original holder
    # alive.
    table = FakeProcessTable({100: 5000.0})
    table.kill(100)
    table.reuse(100, 8000.0)
    assert not table.is_alive(100, 5000.0)
    # The reused process is alive in its own right, of course.
    assert table.is_alive(100, 8000.0)


def test_fake_spawn_and_capture() -> None:
    table = FakeProcessTable()
    table.spawn(200, 1234.5)
    identity = ProcessIdentity.capture(table, 200)
    assert identity == ProcessIdentity(pid=200, create_time=1234.5)
    assert table.is_alive(identity.pid, identity.create_time)


def test_fake_capture_missing_pid_raises() -> None:
    table = FakeProcessTable()
    with pytest.raises(LookupError):
        ProcessIdentity.capture(table, 12345)


def test_fake_create_time_tolerates_small_float_drift() -> None:
    # create_time is float seconds; production checks tolerance 1e-3 to absorb
    # floating-point round-trips through JSON.
    table = FakeProcessTable({1: 1000.0001})
    assert table.is_alive(1, 1000.0)


# --- pids_for_socket: orphan-daemon detection ---


def test_fake_pids_for_socket_returns_only_attached_alive_pids() -> None:
    table = FakeProcessTable({10: 1.0, 20: 2.0})
    table.attach_socket(10, "/run/a.sock")
    table.attach_socket(20, "/run/b.sock")
    assert table.pids_for_socket("/run/a.sock") == [10]
    assert table.pids_for_socket("/run/b.sock") == [20]
    assert table.pids_for_socket("/run/missing.sock") == []


def test_fake_pids_for_socket_drops_killed_pid() -> None:
    table = FakeProcessTable({10: 1.0})
    table.attach_socket(10, "/run/a.sock")
    table.kill(10)
    assert table.pids_for_socket("/run/a.sock") == []


def test_psutil_pids_for_socket_finds_self_by_synthetic_arg() -> None:
    # The current pytest process won't carry a --socket=… arg, so an arbitrary
    # path yields nothing; this just exercises the psutil enumeration path.
    table = PsutilProcessTable()
    assert table.pids_for_socket("/nonexistent/tailctl.sock") == []
