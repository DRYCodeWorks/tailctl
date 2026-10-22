"""InstanceManager tests — orchestration with fully faked OS seams.

No real tailscaled, no real subprocesses: spawn_daemon/spawn_forwarder/spawn_login
are fakes that register pids in a FakeProcessTable, and the Tailscale client is a
scripted fake whose BackendState the test controls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tailctl import paths
from tailctl.config import Config
from tailctl.instance_manager import (
    InstanceError,
    InstanceManager,
    ShieldsReport,
    _default_secret_resolve,
)
from tailctl.liveness import FakeProcessTable
from tailctl.profiles import PortForward, Profile, Profiles
from tailctl.tailscale import TailscaleError


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
                auth_key_ssm="/client/sazabi/KEYED_TS_KEY",
            ),
        },
    )
    return Config(profiles=profiles, tailscale_binary="ts", tailscaled_binary="tsd")


class FakeClient:
    """status() reflects a shared mutable state cell so tests flip Running."""

    def __init__(self, socket: str, state: list[str], shields: dict | None = None) -> None:
        self._socket = socket
        self._state = state  # one-element mutable list
        # Shared shields cell: "up" is the pref, "stuck" makes raising a no-op,
        # "error" makes every read fail, "raised" counts raise attempts.
        self._shields = shields or _shields_cell()
        self.exit_node_set: str | None = None
        self.logged_out = False
        self.up_calls: list[dict] = []  # may be replaced with a shared list

    def status(self):  # noqa: ANN201
        st = self._state[0]
        raw = {} if st == "Running" else {"AuthURL": "https://login.example/abc"}
        return SimpleNamespace(backend_state=st, raw=raw)

    def shields_up(self) -> bool:
        if self._shields["error"]:
            raise TailscaleError("debug prefs failed (rc=1): no daemon")
        return self._shields["up"]

    def raise_shields(self) -> None:
        self._shields["raised"] += 1
        if not self._shields["stuck"]:
            self._shields["up"] = True

    def up(self, *, auth_key=None, **kw):  # noqa: ANN001, ANN201
        self.up_calls.append({"auth_key": auth_key, **kw})
        if auth_key:
            self._state[0] = "Running"  # non-interactive auth succeeded
        return (0, "", "")

    def set_exit_node(self, value):  # noqa: ANN001
        self.exit_node_set = value

    def logout(self) -> None:
        self.logged_out = True


def _shields_cell() -> dict:
    return {"up": True, "stuck": False, "error": False, "raised": 0}


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TAILCTL_HOME", str(tmp_path))
    return tmp_path


def _manager(state: list[str], pt: FakeProcessTable, secret_resolve=None,  # noqa: ANN001
             linger=0.0, now_epoch=None):  # noqa: ANN001
    # Spawned daemon/forwarder pids are passed to the real os.kill in _kill;
    # use values above the OS max pid so os.kill always raises ProcessLookupError
    # (never signals a coincidental real process during teardown tests).
    next_pid = [900001]
    spawned = {"daemons": [], "forwarders": [], "logins": 0, "login_argvs": []}

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
        spawned["login_argvs"].append(argv)

    clock = [0.0]

    def tick():
        clock[0] += 0.1
        return clock[0]

    up_calls: list[dict] = []
    spawned["up_calls"] = up_calls
    shields = _shields_cell()
    spawned["shields"] = shields

    def make_client(sock):  # noqa: ANN001
        c = FakeClient(sock, state, shields)
        c.up_calls = up_calls  # shared across the per-call clients
        return c

    mgr = InstanceManager(
        _config(),
        process_table=pt,
        spawn_daemon=spawn_daemon,
        spawn_forwarder=spawn_forwarder,
        spawn_login=spawn_login,
        make_client=make_client,
        secret_resolve=secret_resolve or (lambda profile: None),
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
    """Absent env and unavailable SSM preserve interactive fallback."""
    monkeypatch.delenv("KEYED_TS_KEY", raising=False)
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["NeedsLogin"], pt)

    result = _up(mgr, "keyed")

    assert result.needs_auth
    assert spawned["logins"] == 1
    assert "--shields-up" in spawned["login_argvs"][0]


def test_auth_key_resolved_from_ssm_when_env_absent(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KEYED_TS_KEY", raising=False)
    state = ["NeedsLogin"]
    mgr, spawned = _manager(
        state,
        FakeProcessTable({5000: 500.0}),
        secret_resolve=lambda profile: (
            "tskey-from-ssm" if profile.name == "keyed" else None
        ),
    )
    result = _up(mgr, "keyed")
    assert result.ready and not result.needs_auth
    assert any(c["auth_key"] == "tskey-from-ssm" for c in spawned["up_calls"])
    assert spawned["logins"] == 0


def test_env_auth_key_skips_ssm_resolver(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYED_TS_KEY", "env-key")
    calls = []
    mgr, _ = _manager(
        ["NeedsLogin"],
        FakeProcessTable({5000: 500.0}),
        secret_resolve=lambda profile: calls.append(profile),
    )
    _up(mgr, "keyed")
    assert calls == []


def test_default_ssm_resolver_scrubs_aws_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _config().profiles.get("keyed")
    monkeypatch.setenv("AWS_ENDPOINT_URL_SSM", "https://attacker.invalid")
    monkeypatch.setenv("AWS_PROFILE", "attacker")
    monkeypatch.setattr("tailctl.instance_manager.shutil.which", lambda name: "/fake/aws")
    observed = {}

    def run(argv, **kwargs):  # noqa: ANN001, ANN201
        observed["argv"] = argv
        observed["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "Parameter": {
                        "Name": profile.auth_key_ssm,
                        "Value": "ssm-key",
                    }
                }
            ),
        )

    monkeypatch.setattr("tailctl.instance_manager.subprocess.run", run)
    assert _default_secret_resolve(profile) == "ssm-key"
    assert not any(key.startswith("AWS_") for key in observed["env"])
    assert "--profile" in observed["argv"]
    assert "secrets-sazabi" in observed["argv"]
    assert os.environ["AWS_PROFILE"] == "attacker"


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


# --- shields (a userspace node delivers inbound to this host's 127.0.0.1) ---


def test_up_raises_shields_a_reconnected_login_left_down(tmp_home: Path) -> None:
    """A saved login that reaches Running on its own skips `tailscale up`, so
    its stored prefs decide shields. up() must read them back and raise them."""
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["Running"], pt)
    spawned["shields"]["up"] = False

    result = _up(mgr, "acme")

    assert result.ready
    assert spawned["shields"]["raised"] == 1
    assert spawned["shields"]["up"] is True


def test_up_refuses_and_kills_daemon_when_shields_wont_raise(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["Running"], pt)
    spawned["shields"].update(up=False, stuck=True)
    killed = _spy_kill(mgr, pt)

    with pytest.raises(InstanceError, match="shields still down"):
        _up(mgr, "acme")

    assert spawned["daemons"][0] in killed
    assert spawned["forwarders"] == []  # never got as far as forwards
    assert mgr.list_instances() == []


def test_up_refuses_when_shields_cannot_be_read(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["Running"], pt)
    spawned["shields"]["error"] = True

    with pytest.raises(InstanceError, match="could not verify shields"):
        _up(mgr, "acme")

    assert mgr.list_instances() == []


def test_reuse_raises_shields_flipped_after_spawn(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0, 6000: 600.0})
    mgr, spawned = _manager(["Running"], pt)
    _up(mgr, "acme", OWNER)
    spawned["shields"]["up"] = False  # e.g. `tailscale set --shields-up=false`

    result = _up(mgr, "acme", OWNER2)

    assert result.ready
    assert spawned["shields"]["up"] is True
    assert len(spawned["daemons"]) == 1  # reused, not respawned


def test_reuse_tears_down_instance_whose_shields_wont_raise(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0, 6000: 600.0})
    mgr, spawned = _manager(["Running"], pt)
    _up(mgr, "acme", OWNER)
    killed = _spy_kill(mgr, pt)
    spawned["shields"].update(up=False, stuck=True)

    with pytest.raises(InstanceError, match="shields still down"):
        _up(mgr, "acme", OWNER2)

    assert spawned["daemons"][0] in killed
    assert spawned["forwarders"][0][0] in killed
    mgr.reap()  # the dead daemon's row goes on the next reap
    assert mgr.list_instances() == []


def test_shields_reports_without_fixing(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["Running"], pt)
    _up(mgr, "acme")
    spawned["shields"]["up"] = False

    assert mgr.shields() == [ShieldsReport("acme", "down")]
    assert spawned["shields"]["raised"] == 0


def test_shields_fix_raises_then_reports_up(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["Running"], pt)
    _up(mgr, "acme")
    spawned["shields"]["up"] = False

    assert mgr.shields(fix=True) == [ShieldsReport("acme", "raised")]
    assert mgr.shields() == [ShieldsReport("acme", "up")]


def test_shields_fix_stops_instance_it_cannot_confirm(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["Running"], pt)
    _up(mgr, "acme")
    killed = _spy_kill(mgr, pt)
    spawned["shields"].update(up=False, stuck=True)

    assert mgr.shields(fix=True) == [ShieldsReport("acme", "stopped")]
    assert spawned["daemons"][0] in killed
    assert mgr.list_instances() == []


def test_shields_unknown_when_daemon_does_not_answer(tmp_home: Path) -> None:
    pt = FakeProcessTable({5000: 500.0})
    mgr, spawned = _manager(["Running"], pt)
    _up(mgr, "acme")
    spawned["shields"]["error"] = True

    assert mgr.shields() == [ShieldsReport("acme", "unknown")]
