"""Spawn, supervise, and refcount per-profile userspace tailscaled instances.

This is the heart of the userspace-per-identity model — the replacement for
``coordinator.py``. There is no global switching and no FIFO queue: each profile
that a session needs gets its OWN headless ``tailscaled`` (userspace networking,
own socket/statedir, own SOCKS5 + HTTP proxy ports), and any number run at once.

``up(profile)``    ensure a live instance exists; reuse + refcount++ if so, else
                   spawn the daemon, drive ``tailscale up`` (surfacing the auth
                   URL on first login), start the configured port-forwards, and
                   register it.
``down(profile)``  refcount--; at zero, stop forwards, log out, kill the daemon,
                   and drop the registry row + runtime dir.
``reap()``         drop registry rows whose daemon pid is dead (lazy cleanup).
``list()``         snapshot of running instances for ``tailctl ps``.

All OS interaction goes through injectable seams (``spawn_daemon``,
``spawn_forwarder``, ``make_client``, ``process_table``, ``sleep``/``clock``) so
the orchestration is unit-testable without a real tailscaled.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket as _socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from tailctl import paths
from tailctl.config import Config
from tailctl.liveness import ProcessTable, PsutilProcessTable
from tailctl.profiles import Profile
from tailctl.registry import Forward, Holder, Instance, RegistryStore
from tailctl.tailscale import TailscaleClient, TailscaleError

# Type of a daemon-spawn seam: (argv, log_path) -> pid
SpawnDaemon = Callable[[list[str], str], int]
# Type of a forwarder-spawn seam: (local_port, socks_port, host, port, log_path) -> pid
SpawnForwarder = Callable[[int, int, str, int, str], int]
# Type of a client factory bound to a socket.
MakeClient = Callable[[str], TailscaleClient]


@dataclass
class UpResult:
    """Outcome of ``InstanceManager.up``."""

    profile: str
    ready: bool
    socks_port: int | None = None
    http_port: int | None = None
    forwards: dict[str, int] | None = None  # service -> local_port
    needs_auth: bool = False
    auth_url: str | None = None


class InstanceError(RuntimeError):
    """Instance lifecycle failure surfaced to the CLI."""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _free_port() -> int:
    """Ask the OS for an unused localhost TCP port."""
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _scrub_key(text: str) -> str:
    """Redact any Tailscale auth key (tskey-...) before surfacing CLI output."""
    cleaned = re.sub(r"tskey-[A-Za-z0-9-]+", "tskey-***", text or "")
    # Keep it to the last non-empty line — tailscale prints the actionable
    # error there; earlier lines are usually banner/noise.
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _spawn_detached(argv: list[str], log_path: str) -> int:
    """Spawn a detached, log-redirected child and return its pid.

    The parent's copy of the log fd is closed immediately after Popen — the
    child inherits its own dup, so keeping the parent fd open just leaks a
    descriptor per spawn (→ EMFILE in a long-lived agent). One shared helper so
    the detach discipline (new session, DEVNULL stdin, unbuffered append log)
    lives in exactly one place.
    """
    log = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from tailctl's process group
        )
    finally:
        log.close()
    return proc.pid


def _default_spawn_daemon(argv: list[str], log_path: str) -> int:
    return _spawn_detached(argv, log_path)


def _default_spawn_forwarder(
    local_port: int, socks_port: int, remote_host: str, remote_port: int, log_path: str
) -> int:
    return _spawn_detached(
        [
            sys.executable,
            "-m",
            "tailctl.forwarder",
            str(local_port),
            str(socks_port),
            remote_host,
            str(remote_port),
        ],
        log_path,
    )


def _default_spawn_login(argv: list[str], log_path: str) -> None:
    _spawn_detached(argv, log_path)


def _default_schedule_reap(linger_seconds: float) -> None:
    """Detached self-reaper: after the linger window, run `tailctl reap` once so
    a lingering daemon is bounded even if no other tailctl command ever runs."""
    delay = int(linger_seconds) + 5
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.Popen(
            ["sh", "-c", f"sleep {delay}; exec tailctl reap >/dev/null 2>&1"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _default_bws_resolve(secret_key: str) -> str | None:
    """Fetch a secret value from Bitwarden Secrets Manager by its key name.

    Used as the fallback when the auth-key env var isn't already injected into
    tailctl's environment — lets a bare ``tailctl up`` self-resolve the key via
    the ambient ``BWS_ACCESS_TOKEN`` (or bws config), instead of requiring a
    ``bws run --`` wrapper. Returns None if bws is absent, unauthenticated, or
    the key isn't found. The secret value is never logged.
    """
    if not shutil.which("bws"):
        return None
    try:
        proc = subprocess.run(
            ["bws", "secret", "list", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        for secret in json.loads(proc.stdout):
            if isinstance(secret, dict) and secret.get("key") == secret_key:
                val = secret.get("value")
                return val if isinstance(val, str) and val else None
    except (json.JSONDecodeError, TypeError):
        return None
    return None


class InstanceManager:
    def __init__(
        self,
        config: Config,
        *,
        store: RegistryStore | None = None,
        process_table: ProcessTable | None = None,
        spawn_daemon: SpawnDaemon = _default_spawn_daemon,
        spawn_forwarder: SpawnForwarder = _default_spawn_forwarder,
        spawn_login: Callable[[list[str], str], None] = _default_spawn_login,
        make_client: MakeClient | None = None,
        bws_resolve: Callable[[str], str | None] = _default_bws_resolve,
        schedule_reap: Callable[[float], None] = _default_schedule_reap,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now_epoch: Callable[[], float] = time.time,
        ready_timeout_s: float = 15.0,
        auth_url_timeout_s: float = 10.0,
        linger_seconds: float = 30.0,
    ) -> None:
        self._config = config
        self._store = store or RegistryStore()
        self._procs = process_table or PsutilProcessTable()
        self._spawn_daemon = spawn_daemon
        self._spawn_forwarder = spawn_forwarder
        self._spawn_login = spawn_login
        # Default client factory binds the configured control CLI to the
        # instance's socket (no hardcoded path; non-brew installs configurable).
        self._make_client = make_client or (
            lambda sock: TailscaleClient(
                binary=config.tailscale_cli_binary, socket=sock
            )
        )
        self._bws_resolve = bws_resolve
        self._schedule_reap = schedule_reap
        self._sleep = sleep
        self._clock = clock
        self._now_epoch = now_epoch
        self._ready_timeout = ready_timeout_s
        self._auth_url_timeout = auth_url_timeout_s
        self._linger = linger_seconds

    # === public API ========================================================

    def up(
        self, profile_name: str, *, owner_pid: int, owner_create_time: float
    ) -> UpResult:
        profile = self._config.profiles.get(profile_name)

        # Fast path: reuse a live instance if one already exists.
        with self._store.transaction() as reg:
            self._reap_inplace(reg)
            res = self._try_reuse(reg, profile, owner_pid, owner_create_time)
            if res is not None:
                return res

        # Slow path. Serialize per-profile so two concurrent `up`s don't both
        # spawn a daemon (the registry lock is NOT held across the slow spawn,
        # and the registry is keyed by a single profile name — without this the
        # loser's daemon becomes an untracked orphan). Re-check under the spawn
        # lock: another `up` may have registered the instance while we waited.
        spawn_fd = self._acquire_spawn_lock(profile_name)
        try:
            with self._store.transaction() as reg:
                self._reap_inplace(reg)
                res = self._try_reuse(reg, profile, owner_pid, owner_create_time)
                if res is not None:
                    return res
            return self._spawn_and_register(profile, owner_pid, owner_create_time)
        finally:
            self._release_spawn_lock(spawn_fd)

    def _try_reuse(
        self, reg, profile: Profile, owner_pid: int, owner_create_time: float
    ) -> UpResult | None:
        """If a live instance for the profile exists, claim+return it; else None.
        Adds the caller as a holder in BOTH the running and pending cases so a
        reused instance is never reaped out from under a new claimant."""
        inst = reg.instances.get(profile.name)
        if inst is None or not self._alive(inst.pid, inst.create_time):
            return None
        client = self._make_client(inst.socket)
        if self._is_running(client):
            # Finalize a previously-pending (just-authed) instance exactly once.
            if not inst.finalized:
                if profile.exit_node:
                    self._set_exit_node_with_retry(client, profile)  # raises on hard fail
                inst.forwards = self._start_forwards(
                    profile, paths.instance_dir(profile.name), inst.socks_port
                )
                inst.finalized = True
            self._add_holder(inst, owner_pid, owner_create_time)
            return self._ready_result(profile.name, inst)
        # Exists but not Running yet (auth pending) — hold it so reap won't kill
        # it while this caller is mid-authentication, then surface the auth URL.
        self._add_holder(inst, owner_pid, owner_create_time)
        return self._auth_result(profile.name, client)

    def _acquire_spawn_lock(self, profile_name: str) -> int:
        lock_path = paths.instance_dir(profile_name) / "spawn.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _try_acquire_spawn_lock(self, profile_name: str) -> int | None:
        """Non-blocking variant: return the held fd, or None if a spawn for this
        profile is in progress (lock contended)."""
        lock_path = paths.instance_dir(profile_name) / "spawn.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd

    def _release_spawn_lock(self, fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def down(self, profile_name: str, *, owner_pid: int) -> None:
        with self._store.transaction() as reg:
            inst = reg.instances.get(profile_name)
            if inst is None:
                return  # idempotent
            inst.holders = [h for h in inst.holders if h.owner_pid != owner_pid]
            if inst.holders:
                return  # other live claims remain
            if self._linger > 0:
                # Keep the warm daemon for a linger window so a quick follow-up
                # `up`/`run` reuses it instead of paying the cold respawn.
                inst.released_at = self._now_epoch()
                # Bound the linger: schedule a detached `tailctl reap` so the
                # daemon is stopped even if no other tailctl command ever runs
                # (otherwise reap, which only fires on command entry, might
                # never run and the daemon would leak indefinitely).
                self._schedule_reap(self._linger)
                return
            self._teardown_instance(inst)
            del reg.instances[profile_name]

    def reap(self) -> list[str]:
        with self._store.transaction() as reg:
            reaped = self._reap_inplace(reg)
        # Untracked-orphan sweep runs after the registry transaction so it can
        # take per-profile spawn locks without inverting the lock order.
        reaped.extend(self._sweep_untracked_orphans())
        return reaped

    def teardown_all(self) -> int:
        """Force-stop every instance regardless of holders (for `reset`)."""
        with self._store.transaction() as reg:
            names = list(reg.instances)
            for name in names:
                self._teardown_instance(reg.instances[name])
                del reg.instances[name]
            return len(names)

    def list_instances(self) -> list[Instance]:
        reg = self._store.read()
        return list(reg.instances.values())

    # === internals =========================================================

    def _spawn_and_register(
        self, profile: Profile, owner_pid: int, owner_create_time: float
    ) -> UpResult:
        rundir = paths.instance_dir(profile.name)
        (rundir / "state").mkdir(parents=True, exist_ok=True)
        sock = str(rundir / "tailscaled.sock")
        statedir = str(rundir / "state")
        # A prior daemon may have leaked while still bound to this socket (spawn
        # race, a row dropped without killing the pid, create_time drift). It
        # would fail our fresh `--socket` bind with "address already in use", so
        # clear it before spawning.
        self._kill_socket_orphans(sock)
        socks_port = _free_port()
        http_port = _free_port()
        log_path = str(rundir / "tailscaled.log")

        argv = [
            self._config.tailscaled_binary,
            "--tun=userspace-networking",
            f"--socket={sock}",
            f"--statedir={statedir}",
            f"--socks5-server=127.0.0.1:{socks_port}",
            f"--outbound-http-proxy-listen=127.0.0.1:{http_port}",
            "--port=0",
        ]
        pid = self._spawn_daemon(argv, log_path)
        try:
            create_time = self._procs.create_time(pid)
        except Exception as exc:  # noqa: BLE001
            raise InstanceError(f"daemon for {profile.name!r} died immediately: {exc}") from exc

        client = self._make_client(sock)
        try:
            self._wait_socket_reachable(client, profile.name)
        except InstanceError:
            self._kill(pid)  # don't leak the just-spawned daemon
            self._cleanup_socket(sock)
            raise

        # Drive `up` (idempotent): an already-authed statedir reconnects and
        # converges to Running; an unauthed one prints an auth URL. We can't
        # tell which yet, so kick it then wait for convergence — a reconnecting
        # authed daemon passes through NeedsLogin/Starting before Running, so a
        # bare is-Running check here would wrongly conclude "needs auth".
        if not self._is_running(client):
            auth_key = self._resolve_auth_key(profile)
            if auth_key:
                # Non-interactive first login — no browser URL. Synchronous:
                # `up --auth-key` returns once the node is registered. Do NOT
                # set the exit node here: a brand-new node hasn't synced its
                # netmap yet, so `up --exit-node=<peer>` can fail with "no such
                # node" and abort the whole registration. The exit node is set
                # after the daemon reaches Running (finalize block below).
                rc, out, err = 1, "", ""
                try:
                    rc, out, err = client.up(
                        accept_routes=profile.accept_routes,
                        accept_dns=False,
                        hostname="tailctl-" + profile.name,
                        auth_key=auth_key,
                    )
                except TailscaleError as exc:
                    err = str(exc)
                if self._wait_running_or_auth(client) != "running":
                    # A key was supplied but login didn't reach Running — this
                    # is a real auth failure (expired/wrong-tailnet/single-use/
                    # untagged), NOT "needs interactive login". Tear down the
                    # orphaned daemon and surface why.
                    self._kill(pid)
                    self._cleanup_socket(sock)
                    raise InstanceError(
                        f"auth-key login for {profile.name!r} failed: "
                        f"{_scrub_key(err or out) or 'tailscale up did not reach Running'}. "
                        f"Verify ${profile.auth_key_env} is a valid, reusable key for "
                        f"{profile.tailnet!r}. Daemon log: {log_path}"
                    )
            else:
                self._kick_login(profile, client, log_path)
                if self._wait_running_or_auth(client) != "running":
                    # Register a pending instance held by the caller so a re-run
                    # reuses it, and reap cleans it up if the caller dies before
                    # authenticating.
                    self._register_pending(
                        profile, pid, create_time, sock, statedir, socks_port,
                        http_port, owner_pid, owner_create_time,
                    )
                    return self._auth_result(profile.name, client)

        # Running: set exit node (if any), start forwards, register. The node
        # is Running but its netmap may still be syncing the exit-node peer, so
        # retry until the hostname resolves. If it ultimately can't be applied,
        # _set_exit_node_with_retry RAISES — we must NOT register a ready
        # instance that routes through the wrong/no exit node, so tear the
        # just-spawned daemon down and propagate.
        if profile.exit_node:
            try:
                self._set_exit_node_with_retry(client, profile)
            except InstanceError:
                self._kill(pid)
                self._cleanup_socket(sock)
                raise
        forwards = self._start_forwards(profile, rundir, socks_port)
        inst = Instance(
            profile=profile.name,
            pid=pid,
            create_time=create_time,
            socket=sock,
            statedir=statedir,
            socks_port=socks_port,
            http_port=http_port,
            created_at=_now_iso(),
            forwards=forwards,
            holders=[Holder(owner_pid, owner_create_time, _now_iso())],
        )
        with self._store.transaction() as reg:
            reg.instances[profile.name] = inst
        return self._ready_result(profile.name, inst)

    def _kick_login(self, profile: Profile, client: TailscaleClient, log_path: str) -> None:
        """Run `tailscale up` detached so it can block on interactive auth while
        we poll for the AuthURL.

        No ``--exit-node`` here: a freshly-spawned node hasn't synced its netmap,
        so Tailscale can't resolve the exit-node hostname and rejects the whole
        ``up`` ("invalid value ... must be IP or hostname"). The exit node is set
        from the finalize block once the node is Running and peers are visible.
        """
        argv = [
            self._config.tailscale_cli_binary,
            f"--socket={client._socket}",  # noqa: SLF001 (same package seam)
            "up",
            f"--accept-routes={'true' if profile.accept_routes else 'false'}",
            "--accept-dns=false",
            "--hostname=tailctl-" + profile.name,
        ]
        self._spawn_login(argv, log_path)

    def _start_forwards(
        self, profile: Profile, rundir, socks_port: int
    ) -> list[Forward]:
        forwards: list[Forward] = []
        for pf in profile.port_forwards:
            lp = pf.local_port or _free_port()
            log_path = str(rundir / f"forward-{pf.service}.log")
            fpid = self._spawn_forwarder(lp, socks_port, pf.remote_host, pf.remote_port, log_path)
            fct: float | None
            try:
                fct = self._procs.create_time(fpid)
            except Exception:  # noqa: BLE001
                fct = None
            forwards.append(
                Forward(
                    service=pf.service,
                    local_port=lp,
                    remote_host=pf.remote_host,
                    remote_port=pf.remote_port,
                    pid=fpid,
                    create_time=fct,
                )
            )
        # Best-effort readiness check: a forwarder whose local port can't bind
        # exits almost immediately, so a short pause + liveness probe catches the
        # common failure (port already in use) rather than recording a dead
        # forward as live. Not a guarantee the listener is accepting yet.
        if forwards:
            self._sleep(0.3)
            for f in forwards:
                if f.pid is not None and f.create_time is not None and not self._procs.is_alive(
                    f.pid, f.create_time
                ):
                    print(
                        f"warning: {profile.name}: forward {f.service!r} "
                        f"(127.0.0.1:{f.local_port}) failed to start — check "
                        f"{rundir}/forward-{f.service}.log (port in use?).",
                        file=sys.stderr,
                    )
        return forwards

    def _teardown_instance(self, inst: Instance) -> None:
        # Stop the daemon + forwarders but DO NOT log out: the statedir keeps
        # the node's login so a later `up` reconnects without re-authenticating.
        # (Deregistering the node is a separate, explicit action.) The socket
        # file IS removed — a stale UDS would otherwise collide with the next
        # spawn (bind failure or a false "reachable" against the dead socket).
        for fwd in inst.forwards:
            if fwd.pid:
                self._kill(fwd.pid)
        self._kill(inst.pid)
        self._cleanup_socket(inst.socket)

    def _cleanup_socket(self, socket_path: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(socket_path)

    def _kill_socket_orphans(
        self, socket_path: str, *, keep_pid: int | None = None
    ) -> list[int]:
        """Kill any tailscaled bound to ``socket_path`` except ``keep_pid``.

        A leaked daemon keeps its UDS bound, so the next ``--socket`` spawn
        fails with ``address already in use`` (the symptom that wedges ``up``).
        Leaks arise from a spawn race, a registry row dropped without killing
        the pid, or create_time drift hiding the daemon from the two-factor
        liveness check. Returns the pids killed; cleans the stale socket if any
        were."""
        killed: list[int] = []
        for pid in self._procs.pids_for_socket(socket_path):
            if keep_pid is not None and pid == keep_pid:
                continue
            self._kill(pid)
            killed.append(pid)
        if killed:
            self._cleanup_socket(socket_path)
        return killed

    def _register_pending(
        self, profile, pid, create_time, sock, statedir, socks_port, http_port,
        owner_pid, owner_create_time,
    ) -> None:
        with self._store.transaction() as reg:
            reg.instances[profile.name] = Instance(
                profile=profile.name,
                pid=pid,
                create_time=create_time,
                socket=sock,
                statedir=statedir,
                socks_port=socks_port,
                http_port=http_port,
                created_at=_now_iso(),
                forwards=[],
                finalized=False,  # awaiting first login; finalize on re-up
                # Held by the caller so it survives until they re-up (after auth)
                # — and is reaped if the caller dies first.
                holders=[Holder(owner_pid, owner_create_time, _now_iso())],
            )

    def _add_holder(self, inst: Instance, owner_pid: int, owner_create_time: float) -> None:
        """Add a claim for an owner, de-duplicating by owner_pid alone.

        One owning process == one holder; dedup on pid (not pid+create_time)
        avoids appending a duplicate when a re-read create_time differs by a
        float ULP, which `down` (filters by pid) would otherwise fail to fully
        release."""
        inst.released_at = None  # reclaimed → cancel any linger countdown
        for h in inst.holders:
            if h.owner_pid == owner_pid:
                h.owner_create_time = owner_create_time  # refresh
                return
        inst.holders.append(Holder(owner_pid, owner_create_time, _now_iso()))

    def _reap_inplace(self, reg) -> list[str]:
        """Drop dead-owner holders; stop+drop any instance whose daemon died,
        whose owner crashed, or whose graceful linger window has expired.

        - daemon dead → drop (clean up forwarders).
        - no live holders + no ``released_at`` → owner crashed without `down`:
          stop immediately (crash-safety; no grace).
        - no live holders + ``released_at`` within linger → keep (warm reuse).
        - no live holders + linger expired → stop.
        """
        reaped: list[str] = []
        for name in list(reg.instances):
            inst = reg.instances[name]
            inst.holders = [
                h
                for h in inst.holders
                if self._procs.is_alive(h.owner_pid, h.owner_create_time)
            ]
            # Dead daemon → always drop (clean up forwarders), regardless of holders.
            if not self._alive(inst.pid, inst.create_time):
                for fwd in inst.forwards:
                    if fwd.pid:
                        self._kill(fwd.pid)
                # "Dead" by the two-factor check can still mean a process holds
                # the socket (create_time drift, or PID reuse by an unrelated
                # daemon on the same path). Kill whatever's bound so the row's
                # socket is free for the next spawn.
                self._kill_socket_orphans(inst.socket)
                del reg.instances[name]
                reaped.append(name)
                continue
            if inst.holders:
                continue  # actively held
            lingering = (
                inst.released_at is not None
                and (self._now_epoch() - inst.released_at) < self._linger
            )
            if lingering:
                continue  # warm, within the linger window
            # No live holders, daemon alive: crashed owner (no released_at) or
            # expired linger → stop.
            self._teardown_instance(inst)
            del reg.instances[name]
            reaped.append(name)
        return reaped

    def _sweep_untracked_orphans(self) -> list[str]:
        """Kill leaked daemons that hold a per-profile socket with no registry
        row (forked-but-died-before-register, or a row dropped without killing
        the pid). ``_reap_inplace`` can't see these — it only iterates rows.

        Runs OUTSIDE the registry transaction and skips any profile whose spawn
        lock is held: a daemon mid-spawn holds its socket but isn't registered
        yet, and must not be mistaken for an orphan and killed. Acquiring the
        spawn lock (not the registry lock) here also preserves the
        spawn-lock-then-registry-lock ordering used by ``up``."""
        swept: list[str] = []
        tracked = {inst.socket for inst in self._store.read().instances.values()}
        for pname in self._config.profiles.names():
            sock = str(paths.instance_dir(pname) / "tailscaled.sock")
            if sock in tracked:
                continue
            fd = self._try_acquire_spawn_lock(pname)
            if fd is None:
                continue  # spawn in progress — not an orphan
            try:
                if self._kill_socket_orphans(sock):
                    swept.append(pname)
            finally:
                self._release_spawn_lock(fd)
        return swept

    # --- small helpers ---

    def _resolve_auth_key(self, profile: Profile) -> str | None:
        """Return the Tailscale auth key for a profile, or None for interactive
        login.

        Resolution order for ``profile.auth_key_env``:
        1. the environment (if something injected it, e.g. ``bws run --``), then
        2. Bitwarden Secrets Manager by that same key name (ambient token), so a
           bare ``tailctl up`` self-resolves without a wrapper.
        Unset everywhere → None (interactive fallback)."""
        if not profile.auth_key_env:
            return None
        return (
            os.environ.get(profile.auth_key_env)
            or self._bws_resolve(profile.auth_key_env)
            or None
        )

    def _alive(self, pid: int, create_time: float) -> bool:
        return self._procs.is_alive(pid, create_time)

    def _is_running(self, client: TailscaleClient) -> bool:
        try:
            return client.status().backend_state == "Running"
        except TailscaleError:
            return False

    def _wait_running_or_auth(self, client: TailscaleClient) -> str:
        """Poll until the daemon reaches Running (authed statedir reconnected)
        or surfaces an AuthURL (needs login). Returns "running" or "auth".

        Distinguishes a genuinely-unauthed daemon from one that merely hasn't
        finished reconnecting yet — without this, a saved login would look like
        it needs re-auth on every `up`.
        """
        deadline = self._clock() + self._ready_timeout
        while True:
            try:
                st = client.status()
                if st.backend_state == "Running":
                    return "running"
                if (st.raw.get("AuthURL") or "").strip():
                    return "auth"
            except TailscaleError:
                pass
            if self._clock() >= deadline:
                return "auth"
            self._sleep(0.25)

    def _discover_exit_node(self, client: TailscaleClient, configured: str) -> str | None:
        """Find the advertising exit-node peer matching the configured name,
        tolerating a changed numeric suffix (e.g. configured ``…-production`` vs
        live ``…-production-2``). Returns the live HostName, or None."""
        try:
            raw = client.status().raw
        except TailscaleError:
            return None
        base = re.sub(r"-\d+$", "", configured)  # strip a trailing -N suffix
        # Match the exact name, or the SAME base with a (different) numeric
        # suffix — NOT an arbitrary prefix. `startswith(base)` would wrongly
        # match e.g. `acme-development-prod` for base `acme-dev` and route
        # through the wrong exit node. Anchored regex: base, or base + "-<N>".
        variant = re.compile(re.escape(base) + r"(-\d+)?$")
        exact: str | None = None
        variants: list[str] = []
        for peer in (raw.get("Peer") or {}).values():
            if not isinstance(peer, dict):
                continue
            host = peer.get("HostName") or ""
            if not (peer.get("ExitNodeOption") and peer.get("Online")):
                continue
            if host == configured:
                exact = host
            elif variant.fullmatch(host):
                variants.append(host)
        # Prefer the exact configured name; else the lexicographically-first
        # advertising variant (deterministic, not dict-iteration order).
        return exact or (sorted(variants)[0] if variants else None)

    def _set_exit_node_with_retry(self, client: TailscaleClient, profile: Profile) -> None:
        """Set the exit node, retrying while the netmap syncs and auto-discovering
        the advertising node by suffix-variant (heals the -2/no-suffix drift).

        RAISES InstanceError if it can't be applied within the window. A profile
        that declares an exit_node but whose traffic would NOT egress through it
        is the cardinal failure this tool exists to prevent — far worse to return
        a 'ready' instance that silently routes through the wrong identity than
        to fail loudly. The caller tears the daemon down on this error."""
        target = profile.exit_node
        deadline = self._clock() + self._ready_timeout
        last_err: TailscaleError | None = None
        discovered = False
        while True:
            try:
                client.set_exit_node(target)
                if target != profile.exit_node:
                    print(
                        f"note: {profile.name}: exit node {profile.exit_node!r} "
                        f"resolved to the advertising node {target!r}.",
                        file=sys.stderr,
                    )
                return
            except TailscaleError as exc:
                last_err = exc
            if not discovered:
                discovered = True
                found = self._discover_exit_node(client, profile.exit_node)
                if found and found != target:
                    target = found
                    continue  # retry immediately with the discovered name
            if self._clock() >= deadline:
                raise InstanceError(
                    f"{profile.name}: could not apply exit node "
                    f"{profile.exit_node!r} ({last_err}). Refusing to proceed — "
                    f"traffic would egress through the wrong identity. Check the "
                    f"name against `tailscale status` (advertising exit nodes)."
                )
            self._sleep(0.5)

    def _wait_socket_reachable(self, client: TailscaleClient, profile_name: str) -> None:
        deadline = self._clock() + self._ready_timeout
        while True:
            try:
                client.status()
                return
            except TailscaleError:
                pass
            if self._clock() >= deadline:
                raise InstanceError(
                    f"tailscaled for {profile_name!r} did not become reachable "
                    f"within {self._ready_timeout}s"
                )
            self._sleep(0.25)

    def _auth_result(self, profile_name: str, client: TailscaleClient) -> UpResult:
        url = self._poll_auth_url(client)
        return UpResult(profile=profile_name, ready=False, needs_auth=True, auth_url=url)

    def _poll_auth_url(self, client: TailscaleClient) -> str | None:
        deadline = self._clock() + self._auth_url_timeout
        while True:
            try:
                raw = client.status().raw
                url = (raw.get("AuthURL") or "").strip()
                if url:
                    return url
            except TailscaleError:
                pass
            if self._clock() >= deadline:
                return None
            self._sleep(0.25)

    def _ready_result(self, profile_name: str, inst: Instance) -> UpResult:
        return UpResult(
            profile=profile_name,
            ready=True,
            socks_port=inst.socks_port,
            http_port=inst.http_port,
            forwards={f.service: f.local_port for f in inst.forwards},
        )

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _kill(self, pid: int) -> None:
        """SIGTERM, wait briefly for graceful exit, then SIGKILL. Daemons run in
        their own session (not our children), so we can't waitpid — poll instead.
        Without escalation an ignored/hung SIGTERM would leave an orphan while
        its registry row is already deleted."""
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = self._clock() + 3.0
        while self._clock() < deadline:
            if not self._pid_alive(pid):
                return
            self._sleep(0.1)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
