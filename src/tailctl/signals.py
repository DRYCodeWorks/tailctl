"""Signal handling for the wait loop.

When a session sitting in ``coord.acquire`` receives SIGINT or SIGTERM (e.g.
the user hits Ctrl-C, or the parent shell kills the tailctl process), we
remove the session from the waiters list under the lock and re-elect the
named handoff if we were the chosen successor. Then we re-raise the default
disposition so the process actually exits.

This module installs handlers on import-time only when ``install_handlers`` is
called — leaving import side-effect free for tests that don't need them.
"""

from __future__ import annotations

import signal
from collections.abc import Callable
from typing import Any

# Callbacks registered by Coordinator.install_signal_handlers (one per wait).
_cleanups: list[Callable[[], None]] = []
_previous_handlers: dict[int, Any] = {}


def push_cleanup(fn: Callable[[], None]) -> None:
    """Register a cleanup to run on SIGINT/SIGTERM. Pops back automatically
    when ``pop_cleanup`` is called."""
    _cleanups.append(fn)


def pop_cleanup() -> None:
    if _cleanups:
        _cleanups.pop()


def _handler(signum: int, frame: Any) -> None:
    while _cleanups:
        try:
            _cleanups.pop()()
        except Exception:
            # Best-effort: keep popping even on individual cleanup failure.
            continue
    # Re-raise default disposition by re-installing the previous handler
    # and re-sending the signal.
    prev = _previous_handlers.get(signum, signal.SIG_DFL)
    signal.signal(signum, prev)
    signal.raise_signal(signum)


def install_handlers() -> None:
    """Install SIGINT + SIGTERM handlers. Safe to call repeatedly."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        _previous_handlers[sig] = signal.signal(sig, _handler)
