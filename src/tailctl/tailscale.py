"""Thin, side-effect-free wrapper around the Tailscale CLI.

All subprocess invocations use ``shell=False`` with an argv list so config
values flow through ``execve`` arguments, never shell strings. Profile names,
account IDs, and exit-node values are validated upstream (config-load time)
against ``^[A-Za-z0-9._-]+$``, so they can pass through without quoting.

This module knows nothing about the registry, holders, or instance lifecycle.
It just runs commands (optionally against a per-instance ``--socket``) and parses
JSON. Higher-level orchestration lives in ``instance_manager.py``.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_BINARY = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

# Convergence + verification timeouts (seconds). Tuned conservative — the
# Round-2 review flagged 5s as unrealistic for account swaps that re-handshake
# WireGuard and fetch a fresh netmap.
DEFAULT_CONVERGENCE_TIMEOUT_S = 20.0
DEFAULT_CONVERGENCE_POLL_INTERVAL_S = 0.5
DEFAULT_SUBPROCESS_TIMEOUT_S = 30.0


# --- exceptions ---


class TailscaleError(RuntimeError):
    """Generic Tailscale CLI failure (non-zero exit, parse error, timeout)."""


class TailscaleConvergenceTimeout(TailscaleError):
    """Tailscale daemon did not converge to the expected state in time."""


# --- types ---


@dataclass(frozen=True)
class SwitchAccount:
    """One row from ``tailscale switch --list --json``."""

    id: str
    nickname: str
    tailnet: str
    account: str
    selected: bool


@dataclass(frozen=True)
class ExitNodeStatus:
    """Minimal projection of ``ExitNodeStatus`` in status --json.

    ``id`` is Tailscale's opaque short ID (e.g. ``nKKJQbqKax11CNTRL``).
    ``hostname`` is the human name from the matching ``Peer`` entry — what
    users put in ``profiles.yaml`` under ``exit_node:``. It's resolved by
    ``parse_status`` joining ``ExitNodeStatus.ID`` against ``Peer[*].ID``;
    ``None`` when the peer entry isn't present (e.g. during reconnect).
    """

    id: str | None
    online: bool
    hostname: str | None = None


@dataclass(frozen=True)
class TailscaleStatus:
    """Minimal projection of ``tailscale status --json`` for our needs."""

    backend_state: str
    current_tailnet_name: str | None
    exit_node: ExitNodeStatus | None
    # The raw dict is preserved so the caller can pull arbitrary fields
    # (e.g. for forensic logging) without re-parsing.
    raw: dict[str, Any]


# --- subprocess seam ---


class CommandRunner(Protocol):
    """Seam for unit tests: replace subprocess with a fake.

    Returns ``(returncode, stdout, stderr)``. Implementations MUST NOT raise
    on non-zero exit — the wrapper decides what's an error.
    """

    def __call__(
        self, argv: list[str], *, timeout: float | None = None
    ) -> tuple[int, str, str]: ...


def real_runner(argv: list[str], *, timeout: float | None = None) -> tuple[int, str, str]:
    """Production runner: ``subprocess.run`` with shell=False, check=False."""
    proc = subprocess.run(
        argv,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


# --- client ---


class TailscaleClient:
    """High-level interface to the Tailscale CLI."""

    def __init__(
        self,
        *,
        binary: str = DEFAULT_BINARY,
        runner: CommandRunner | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        socket: str | None = None,
    ) -> None:
        self._binary = binary
        self._runner = runner or real_runner
        self._sleep = sleep
        self._clock = clock
        # When set, every CLI invocation targets a specific (userspace)
        # tailscaled via its UDS, instead of the machine-global daemon. On
        # macOS the CLI otherwise defaults to [::1]:64511, so this MUST be
        # passed explicitly to drive a per-identity instance.
        self._socket = socket

    def _argv(self, *rest: str) -> list[str]:
        """Build a CLI argv, prepending --socket when this client is bound to
        a specific daemon socket."""
        head = [self._binary]
        if self._socket:
            head.append(f"--socket={self._socket}")
        return [*head, *rest]

    # --- raw probes ---

    def switch_list(self) -> list[SwitchAccount]:
        """Return all signed-in accounts. Equivalent of ``switch --list --json``."""
        rc, stdout, stderr = self._runner(
            self._argv("switch", "--list", "--json"),
            timeout=DEFAULT_SUBPROCESS_TIMEOUT_S,
        )
        if rc != 0:
            raise TailscaleError(f"switch --list --json failed (rc={rc}): {stderr.strip()}")
        return parse_switch_list(stdout)

    def status(self) -> TailscaleStatus:
        """Return ``tailscale status --json`` parsed into a minimal projection."""
        rc, stdout, stderr = self._runner(
            self._argv("status", "--json"),
            timeout=DEFAULT_SUBPROCESS_TIMEOUT_S,
        )
        if rc != 0:
            raise TailscaleError(f"status --json failed (rc={rc}): {stderr.strip()}")
        return parse_status(stdout)

    def active_account(self) -> SwitchAccount | None:
        for acct in self.switch_list():
            if acct.selected:
                return acct
        return None

    # --- mutators ---

    def switch_account(self, account_id: str) -> None:
        """Issue ``tailscale switch <id>``. Does NOT wait for convergence."""
        rc, _stdout, stderr = self._runner(
            self._argv("switch", account_id),
            timeout=DEFAULT_SUBPROCESS_TIMEOUT_S,
        )
        if rc != 0:
            raise TailscaleError(
                f"switch {account_id} failed (rc={rc}): {stderr.strip()}"
            )

    def set_exit_node(self, exit_node: str | None) -> None:
        """Issue ``tailscale set --exit-node=<name>`` (empty = clear)."""
        value = exit_node or ""
        rc, _stdout, stderr = self._runner(
            self._argv("set", f"--exit-node={value}"),
            timeout=DEFAULT_SUBPROCESS_TIMEOUT_S,
        )
        if rc != 0:
            raise TailscaleError(
                f"set --exit-node={value!r} failed (rc={rc}): {stderr.strip()}"
            )

    def up(
        self,
        *,
        accept_routes: bool = True,
        accept_dns: bool = False,
        exit_node: str | None = None,
        hostname: str | None = None,
        auth_key: str | None = None,
        timeout_s: float = DEFAULT_SUBPROCESS_TIMEOUT_S,
    ) -> tuple[int, str, str]:
        """Issue ``tailscale up`` against this client's (userspace) daemon.

        With ``auth_key`` the login is non-interactive and returns once the
        node is registered. Without it, a first-time login blocks/prints an
        auth URL on stdout for the caller to surface; an already-authed
        statedir returns rc=0 immediately. Subprocess timeout still raises.

        The auth key is passed via argv to the tailscale CLI; it is never
        included in this wrapper's logs or error messages.
        """
        argv = self._argv("up")
        argv.append(f"--accept-routes={'true' if accept_routes else 'false'}")
        argv.append(f"--accept-dns={'true' if accept_dns else 'false'}")
        if exit_node:
            argv.append(f"--exit-node={exit_node}")
        if hostname:
            argv.append(f"--hostname={hostname}")
        if auth_key:
            argv.append(f"--auth-key={auth_key}")
        return self._runner(argv, timeout=timeout_s)

    def logout(self) -> None:
        """Deregister this daemon's node (clears the statedir's login)."""
        self._runner(self._argv("logout"), timeout=DEFAULT_SUBPROCESS_TIMEOUT_S)

    # --- convergence ---

    def wait_for_backend_running_and_tailnet(
        self,
        *,
        expected_tailnet: str | None,
        timeout_s: float = DEFAULT_CONVERGENCE_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_CONVERGENCE_POLL_INTERVAL_S,
    ) -> TailscaleStatus:
        """Poll status until ``BackendState == Running`` and (optionally) the
        active tailnet matches ``expected_tailnet``.

        ``expected_tailnet`` may be ``None`` or empty to skip the tailnet
        check (used when the active account has no tailnet name, e.g. a
        personal account).
        """
        deadline = self._clock() + timeout_s
        last: TailscaleStatus | None = None
        while True:
            try:
                last = self.status()
            except TailscaleError:
                last = None
            if last is not None and last.backend_state == "Running":
                if not expected_tailnet or last.current_tailnet_name == expected_tailnet:
                    return last
            if self._clock() >= deadline:
                raise TailscaleConvergenceTimeout(
                    f"backend did not reach Running/{expected_tailnet!r} within "
                    f"{timeout_s}s (last={last!r})"
                )
            self._sleep(poll_interval_s)


# --- parsers ---


def parse_switch_list(stdout: str) -> list[SwitchAccount]:
    """Parse ``switch --list --json`` output into typed records."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TailscaleError(f"switch --list --json: invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise TailscaleError("switch --list --json: expected a JSON array")
    out: list[SwitchAccount] = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise TailscaleError(f"switch --list --json: entry {i} is not an object")
        try:
            out.append(
                SwitchAccount(
                    id=str(entry["id"]),
                    nickname=str(entry.get("nickname", "")),
                    tailnet=str(entry.get("tailnet", "")),
                    account=str(entry.get("account", "")),
                    selected=bool(entry["selected"]),
                )
            )
        except KeyError as exc:
            raise TailscaleError(
                f"switch --list --json: entry {i} missing key {exc.args[0]!r}"
            ) from exc
    return out


def parse_status(stdout: str) -> TailscaleStatus:
    """Parse ``status --json`` into the minimal projection used internally."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TailscaleError(f"status --json: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TailscaleError("status --json: expected a JSON object")
    backend_state = data.get("BackendState")
    if not isinstance(backend_state, str):
        raise TailscaleError("status --json: missing or non-string BackendState")
    tailnet_name: str | None = None
    current_tailnet = data.get("CurrentTailnet")
    if isinstance(current_tailnet, dict):
        name = current_tailnet.get("Name")
        if isinstance(name, str) and name:
            tailnet_name = name
    exit_node: ExitNodeStatus | None = None
    ens = data.get("ExitNodeStatus")
    if isinstance(ens, dict):
        en_id = str(ens["ID"]) if isinstance(ens.get("ID"), str) else None
        exit_node = ExitNodeStatus(
            id=en_id,
            online=bool(ens.get("Online", False)),
            hostname=_resolve_exit_node_hostname(data, en_id),
        )
    return TailscaleStatus(
        backend_state=backend_state,
        current_tailnet_name=tailnet_name,
        exit_node=exit_node,
        raw=data,
    )


def _resolve_exit_node_hostname(
    status_data: dict[str, Any], exit_node_id: str | None
) -> str | None:
    """Join ExitNodeStatus.ID against Peer[*].ID (and Self) to find the
    human hostname.

    Tailscale reports the active exit node by opaque internal ID; users
    write profiles.yaml using the peer's HostName. Without this join, a
    correctly-configured exit node looks like drift.

    Self is checked alongside Peer because a node advertising itself as an
    exit node appears in Self, not Peer — uncommon but legal.

    Returns None when the ID is absent or no node matches (treat as
    unverified — callers should not flag drift in that case).
    """
    if not exit_node_id:
        return None
    candidates: list[dict[str, Any]] = []
    self_node = status_data.get("Self")
    if isinstance(self_node, dict):
        candidates.append(self_node)
    peers = status_data.get("Peer")
    if isinstance(peers, dict):
        candidates.extend(p for p in peers.values() if isinstance(p, dict))
    for node in candidates:
        if node.get("ID") == exit_node_id:
            host = node.get("HostName")
            if isinstance(host, str) and host:
                return host
            return None
    return None
