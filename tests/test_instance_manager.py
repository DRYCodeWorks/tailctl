"""InstanceManager tests — orchestration with fully faked OS seams.

No real tailscaled, no real subprocesses: spawn_daemon/spawn_forwarder/spawn_login
are fakes that register pids in a FakeProcessTable, and the Tailscale client is a
scripted fake whose BackendState the test controls.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tailctl import paths
from tailctl.config import Config
from tailctl.instance_manager import InstanceManager
from tailctl.liveness import FakeProcessTable
from tailctl.profiles import PortForward, Profile, Profiles


def _config() -> Config:
    profiles = Profiles(
        default_name="drycode",
        by_name={
            "drycode": Profile(name="drycode", account_id="a1b2", tailnet="dc"),
            "acme": Profile(
                name="acme",
                account_id="a2c4",
                tailnet="acme.example.ts.net",
                exit_node="subnet-dev",
                port_forwards=(
                    PortForward(
                        service="clickhouse",
                        remote_host="100.64.0.10",
                        remote_port=9000,
                    ),
                ),
            ),
            "keyed": Profile(
                name="keyed",
                account_id="kk00",
                tailnet="keyed.net",
                auth_key_env="KEYED_TS_KEY",
            ),
        },
    )
    return Config(profiles=profiles, tailscale_binary="ts", tailscaled_binary="tsd")


class FakeClient:
    """status() reflects a shared mutable state cell so tests flip Running."""

    def __init__(self, socket: str, state: list[str]) -> None:
        self._socket = socket
        self._state = state  # one-element mutable list
        self.exit_node_set: str | None = None
        self.logged_out = False
        self.up_calls: list[dict] = []  # may be replaced with a shared list

    def status(self):  # noqa: ANN201
        st = self._state[0]
        raw = {} if st == "Running" else {"AuthURL": "https://login.example/abc"}
        return SimpleNamespace(backend_state=st, raw=raw)

    def up(self, *, auth_key=None, **kw):  # noqa: ANN001, ANN201
        self.up_calls.append({"auth_key": auth_key, **kw})
        if auth_key:
            self._state[0] = "Running"  # non-interactive auth succeeded
        return (0, "", "")

    def set_exit_node(self, value):  # noqa: ANN001
        self.exit_node_set = value

    def logout(self) -> None:
        self.logged_out = True


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TAILCTL_HOME", str(tmp_path))
    return tmp_path


def _manager(state: list[str], pt: FakeProcessTable, bws_resolve=None,  # noqa: ANN001
             linger=0.0, now_epoch=None):  # noqa: ANN001
    # Spawned daemon/forwarder pids are passed to the real os.kill in _kill;
    # use values above the OS max pid so os.kill always raises ProcessLookupError
    # (never signals a coincidental real process during teardown tests).
    next_pid = [900001]
    spawned = {"daemons": [], "forwarders": [], "logins": 0}

    def spawn_daemon(argv, log):  # noqa: ANN001
        pid = next_pid[0]
        next_pid[0] += 1
        pt.spawn(pid, float(pid))
        spawned["daemons"].append(pid)
        return pid

    def spawn_forwarder(lp, sp, host, port, log):  # noqa: ANN001
        pid = next_pid[0]
        next_pid[0] += 1
        pt.spawn(pid, float(pid))
        spawned["forwarders"].append((pid, lp, host, port))
        return pid

    def spawn_login(argv, log):  # noqa: ANN001
        spawned["logins"] += 1

    clock = [0.0]

    def tick():
        clock[0] += 0.1
        return clock[0]

    up_calls: list[dict] = []
    spawned["up_calls"] = up_calls

    def make_client(sock):  # noqa: ANN001
        c = FakeClient(sock, state)
        c.up_calls = up_calls  # shared across the per-call clients
        return c

    mgr = InstanceManager(
        _config(),
        process_table=pt,
        spawn_daemon=spawn_daemon,
        spawn_forwarder=spawn_forwarder,
        spawn_login=spawn_login,
        make_client=make_client,
        bws_resolve=bws_resolve or (lambda key: None),  # no real BWS in unit tests
        schedule_reap=lambda s: None,  # don't spawn a real detached reaper
        sleep=lambda s: None,
        clock=tick,
        now_epoch=now_epoch or (lambda: 1000.0),
        linger_seconds=linger,
    )
    return mgr, spawned


# Owners (pid, create_time) — must be present/alive in the FakeProcessTable for
# their holder to survive reap.
OWNER = (5000, 500.0)
OWNER2 = (6000, 600.0)


def _up(mgr, profile, owner=OWNER):  # noqa: ANN001
    return mgr.up(profile, owner_pid=owner[0], owner_create_time=owner[1])


def _down(mgr, profile, owner=OWNER):  # noqa: ANN001
    mgr.down(profile, owner_pid=owner[0])


def test_up_ready_spawns_daemon_forwards_and_registers(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["Running"], pt)

    result = _up(mgr, "acme")

    assert result.ready and not result.needs_auth
    assert result.socks_port and result.http_port
    assert result.forwards == {"clickhouse": spawned["forwarders"][0][1]}
    assert len(spawned["daemons"]) == 1
    assert len(spawned["forwarders"]) == 1  # one port_forward
    inst = mgr.list_instances()[0]
    assert inst.profile == "acme" and inst.refcount == 1


def test_up_reuse_two_owners_share_one_daemon(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0, 6000: 600.0})
    mgr, spawned = _manager(["Running"], pt)
    _up(mgr, "acme", OWNER)
    _up(mgr, "acme", OWNER2)  # second session, same profile
    assert len(spawned["daemons"]) == 1  # shared, not respawned
    assert mgr.list_instances()[0].refcount == 2  # two holders


def test_same_owner_up_twice_is_idempotent(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    mgr, _ = _manager(["Running"], pt)
    _up(mgr, "acme")
    _up(mgr, "acme")  # same owner — does not double-count
    assert mgr.list_instances()[0].refcount == 1


def test_down_releases_per_owner_then_tears_down(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0, 6000: 600.0})
    mgr, _ = _manager(["Running"], pt)
    _up(mgr, "acme", OWNER)
    _up(mgr, "acme", OWNER2)
    _down(mgr, "acme", OWNER)
    assert mgr.list_instances()[0].refcount == 1  # OWNER2 still holds
    _down(mgr, "acme", OWNER2)
    assert mgr.list_instances() == []  # last release removes the row


def test_linger_keeps_warm_daemon_then_reaps_after_window(tmp_home: Path) -> None:
    """With linger>0, the last `down` keeps the daemon warm; a re-up within the
    window reuses it (no respawn); reap after the window stops it."""
    pt = FakeProcessTable({5000: 500.0})
    clock = [1000.0]
    mgr, spawned = _manager(["Running"], pt, linger=30.0, now_epoch=lambda: clock[0])

    _up(mgr, "acme")
    _down(mgr, "acme")  # lingers (not torn down)
    assert len(mgr.list_instances()) == 1  # warm daemon kept
    assert len(spawned["daemons"]) == 1

    clock[0] += 5  # within linger window
    _up(mgr, "acme")  # reuse warm daemon
    assert len(spawned["daemons"]) == 1  # NOT respawned
    _down(mgr, "acme")

    clock[0] += 31  # past the linger window
    reaped = mgr.reap()
    assert reaped == ["acme"]
    assert mgr.list_instances() == []


def test_dead_owner_claim_is_reaped_and_daemon_stopped(tmp_home: Path) -> None:
    """The crash-safety guarantee: an owner that ups then dies without down has
    its claim reaped and the orphaned daemon stopped."""
    pt = FakeProcessTable({5000: 500.0})
    mgr, _ = _manager(["Running"], pt)
    _up(mgr, "acme")
    assert len(mgr.list_instances()) == 1
    pt.kill(5000)  # owner crashed without calling down
    reaped = mgr.reap()
    assert reaped == ["acme"]
    assert mgr.list_instances() == []


def test_up_needs_auth_when_not_running(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    state = ["NeedsLogin"]
    mgr, spawned = _manager(state, pt)

    result = _up(mgr, "acme")

    assert result.needs_auth and not result.ready
    assert result.auth_url == "https://login.example/abc"
    assert spawned["logins"] == 1  # login was kicked
    # A pending instance is registered, held by the caller (so reuse works and
    # reap cleans it up if the caller dies before authenticating).
    inst = mgr.list_instances()[0]
    assert inst.refcount == 1 and inst.forwards == []


def test_pending_then_authed_finalizes_forwards(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    state = ["NeedsLogin"]
    mgr, spawned = _manager(state, pt)
    _up(mgr, "acme")  # pending
    assert spawned["forwarders"] == []  # no forwards while pending

    state[0] = "Running"  # user completed auth
    result = _up(mgr, "acme")
    assert result.ready
    assert len(spawned["forwarders"]) == 1  # forwards started on finalize
    assert mgr.list_instances()[0].refcount == 1


def test_auth_key_enables_non_interactive_login(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With auth_key_env set + present, up() logs in non-interactively (no
    pending/auth result) — the daemon reaches Running via the key."""
    monkeypatch.setenv("KEYED_TS_KEY", "tskey-abc123")
    pt = FakeProcessTable({5000: 500.0})
    state = ["NeedsLogin"]  # would be an auth URL without a key
    mgr, spawned = _manager(state, pt)

    result = _up(mgr, "keyed")

    assert result.ready and not result.needs_auth
    # up() was called WITH the key (and never fell through to interactive login).
    assert any(c["auth_key"] == "tskey-abc123" for c in spawned["up_calls"])
    assert spawned["logins"] == 0  # interactive kick never fired


def test_missing_auth_key_env_falls_back_to_interactive(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auth_key_env set, var absent, BWS has nothing → interactive login."""
    monkeypatch.delenv("KEYED_TS_KEY", raising=False)
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["NeedsLogin"], pt)

    result = _up(mgr, "keyed")

    assert result.needs_auth
    assert spawned["logins"] == 1  # fell back to the interactive kick


def test_auth_key_resolved_from_bws_when_env_absent(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env var absent but BWS has the secret → non-interactive login via the
    BWS-resolved key (no browser, no `bws run` wrapper needed)."""
    monkeypatch.delenv("KEYED_TS_KEY", raising=False)
    pt = FakeProcessTable({5000: 500.0})
    state = ["NeedsLogin"]
    resolved = {"KEYED_TS_KEY": "tskey-from-bws"}
    mgr, spawned = _manager(state, pt, bws_resolve=lambda k: resolved.get(k))

    result = _up(mgr, "keyed")

    assert result.ready and not result.needs_auth
    assert any(c["auth_key"] == "tskey-from-bws" for c in spawned["up_calls"])
    assert spawned["logins"] == 0  # BWS-resolved key, no interactive kick


def test_discover_exit_node_matches_suffix_variant_not_prefix(tmp_home: Path) -> None:
    """Exit-node discovery must match a numeric-suffix variant but NOT an
    arbitrary prefix — picking the wrong peer would silently route through the
    wrong exit node."""
    mgr, _ = _manager(["Running"], FakeProcessTable({}))
    peers = {
        "1": {"HostName": "acme-development-prod", "ExitNodeOption": True, "Online": True},
        "2": {"HostName": "acme-dev-2", "ExitNodeOption": True, "Online": True},
    }

    class PeersClient:
        def status(self):  # noqa: ANN201
            return SimpleNamespace(backend_state="Running", raw={"Peer": peers})

    # configured `acme-dev`: must resolve to the -2 variant, never the
    # prefix-colliding `acme-development-prod`.
    assert mgr._discover_exit_node(PeersClient(), "acme-dev") == "acme-dev-2"


def test_reap_drops_dead_daemon(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["Running"], pt)
    _up(mgr, "acme")
    daemon_pid = spawned["daemons"][0]
    pt.kill(daemon_pid)  # daemon died
    reaped = mgr.reap()
    assert reaped == ["acme"]
    assert mgr.list_instances() == []


# --- orphan socket cleanup (leaked tailscaled wedging "address already in use") ---


def _spy_kill(mgr: InstanceManager, pt: FakeProcessTable) -> list[int]:
    """Replace ``mgr._kill`` with a recorder that also removes the pid from the
    fake table, so subsequent ``pids_for_socket`` reflects the kill."""
    killed: list[int] = []

    def fake_kill(pid: int) -> None:
        killed.append(pid)
        pt.kill(pid)

    mgr._kill = fake_kill  # type: ignore[method-assign]
    return killed


def test_up_kills_socket_orphan_before_spawning(tmp_home: Path) -> None:
    """A leaked daemon still bound to the profile's socket is cleared before the
    fresh spawn — otherwise the new --socket bind fails with EADDRINUSE."""
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["Running"], pt)
    sock = str(paths.instance_dir("acme") / "tailscaled.sock")
    pt.spawn(88669, 88.0)  # orphan from a prior leaked run
    pt.attach_socket(88669, sock)
    killed = _spy_kill(mgr, pt)

    _up(mgr, "acme")

    assert 88669 in killed  # orphan cleared
    assert len(spawned["daemons"]) == 1  # fresh daemon spawned
    assert mgr.list_instances()[0].profile == "acme"


def test_reap_sweeps_untracked_socket_orphan(tmp_home: Path) -> None:
    """The exact wedged-state bug: a daemon holds the profile socket but the
    registry has no row for it. reap must find it by socket path and kill it."""
    pt = FakeProcessTable({})
    mgr, _ = _manager(["Running"], pt)
    sock = str(paths.instance_dir("acme") / "tailscaled.sock")
    pt.spawn(88669, 88.0)
    pt.attach_socket(88669, sock)
    killed = _spy_kill(mgr, pt)

    reaped = mgr.reap()

    assert 88669 in killed
    assert "acme" in reaped


def test_reap_skips_untracked_orphan_while_spawn_in_progress(tmp_home: Path) -> None:
    """A daemon mid-spawn holds its socket but isn't registered yet; the sweep
    must not mistake it for an orphan. Holding the spawn lock simulates that."""
    pt = FakeProcessTable({})
    mgr, _ = _manager(["Running"], pt)
    sock = str(paths.instance_dir("acme") / "tailscaled.sock")
    pt.spawn(900500, 9.0)  # the in-flight (not-yet-registered) daemon
    pt.attach_socket(900500, sock)
    killed = _spy_kill(mgr, pt)

    fd = mgr._acquire_spawn_lock("acme")  # spawn in progress
    try:
        reaped = mgr.reap()
    finally:
        mgr._release_spawn_lock(fd)

    assert 900500 not in killed  # spared while spawn lock held
    assert "acme" not in reaped
