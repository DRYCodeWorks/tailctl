"""Tests for the Tailscale CLI wrapper.

Subprocess is replaced with a scripted fake so behavior is deterministic.
The fake's invocation log lets each test pin "what argv did we call, with
what timeout?" — the key seam for the security-conscious shell=False / argv-
list discipline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tailctl.tailscale import (
    DEFAULT_BINARY,
    SwitchAccount,
    TailscaleClient,
    TailscaleConvergenceTimeout,
    TailscaleError,
    parse_status,
    parse_switch_list,
)

# --- fake runner ---


@dataclass
class _Call:
    argv: list[str]
    timeout: float | None


class ScriptedRunner:
    """CommandRunner test double.

    Construct with a list of responses; each invocation pops the next.
    Records every call for assertions.
    """

    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[_Call] = []

    def __call__(
        self, argv: list[str], *, timeout: float | None = None
    ) -> tuple[int, str, str]:
        self.calls.append(_Call(argv=list(argv), timeout=timeout))
        if not self._responses:
            raise AssertionError(f"unexpected extra call: {argv}")
        return self._responses.pop(0)


def _fake_clock_and_sleep() -> tuple[Callable[[], float], Callable[[float], None], list[float]]:
    """Deterministic clock + sleep recorder, useful for convergence tests."""
    now = [0.0]
    log: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(s: float) -> None:
        log.append(s)
        now[0] += s

    return clock, sleep, log


def _fixture(fixtures_path: Path, name: str) -> str:
    return (fixtures_path / name).read_text()


# --- parse_switch_list ---


def test_parse_switch_list_multi(fixtures_path: Path) -> None:
    data = parse_switch_list(_fixture(fixtures_path, "switch-list-multi.json"))
    assert len(data) == 4
    selected = [a for a in data if a.selected]
    assert len(selected) == 1
    assert selected[0].id == "a1b2"


def test_parse_switch_list_empty(fixtures_path: Path) -> None:
    assert parse_switch_list(_fixture(fixtures_path, "switch-list-empty.json")) == []


def test_parse_switch_list_rejects_non_array() -> None:
    with pytest.raises(TailscaleError, match="expected a JSON array"):
        parse_switch_list('{"not": "array"}')


def test_parse_switch_list_rejects_invalid_json() -> None:
    with pytest.raises(TailscaleError, match="invalid JSON"):
        parse_switch_list("{not json")


def test_parse_switch_list_missing_key_message_is_specific() -> None:
    with pytest.raises(TailscaleError, match="missing key 'selected'"):
        parse_switch_list('[{"id": "x", "tailnet": "y", "account": "z"}]')


# --- parse_status ---


def test_parse_status_ready(fixtures_path: Path) -> None:
    status = parse_status(_fixture(fixtures_path, "status-ready.json"))
    assert status.backend_state == "Running"
    assert status.current_tailnet_name == "default.example"
    assert status.exit_node is None


def test_parse_status_with_exit_node(fixtures_path: Path) -> None:
    status = parse_status(_fixture(fixtures_path, "status-with-exit-node.json"))
    assert status.backend_state == "Running"
    assert status.exit_node is not None
    assert status.exit_node.online is True
    assert status.exit_node.id == "exitNode99"
    # Hostname resolved by joining ExitNodeStatus.ID against Peer[*].ID.
    # This is what makes the drift comparison work against profiles.yaml.
    assert status.exit_node.hostname == "fixture-exit-node"


def test_parse_status_exit_node_hostname_none_when_peer_missing() -> None:
    """If Tailscale advertises an exit node ID but no Peer entry matches
    (transient reconnect / malformed data), hostname is None — callers
    treat that as 'unverified' rather than 'drift'."""
    text = (
        '{"BackendState": "Running",'
        ' "ExitNodeStatus": {"ID": "orphan", "Online": true},'
        ' "Peer": {}}'
    )
    status = parse_status(text)
    assert status.exit_node is not None
    assert status.exit_node.id == "orphan"
    assert status.exit_node.hostname is None


def test_parse_status_exit_node_hostname_none_when_no_peer_dict() -> None:
    """A status with no Peer key at all (some Tailscale states)."""
    text = (
        '{"BackendState": "Running",'
        ' "ExitNodeStatus": {"ID": "x", "Online": true}}'
    )
    status = parse_status(text)
    assert status.exit_node is not None
    assert status.exit_node.hostname is None


def test_parse_status_needslogin(fixtures_path: Path) -> None:
    status = parse_status(_fixture(fixtures_path, "status-needslogin.json"))
    assert status.backend_state == "NeedsLogin"
    assert status.current_tailnet_name is None
    assert status.exit_node is None


def test_parse_status_starting(fixtures_path: Path) -> None:
    status = parse_status(_fixture(fixtures_path, "status-starting.json"))
    assert status.backend_state == "Starting"


def test_parse_status_rejects_missing_backend_state() -> None:
    with pytest.raises(TailscaleError, match="BackendState"):
        parse_status('{"BackendState": null}')


def test_parse_status_preserves_raw_dict(fixtures_path: Path) -> None:
    text = _fixture(fixtures_path, "status-ready.json")
    status = parse_status(text)
    assert status.raw["Self"]["HostName"] == "test-host"


# --- TailscaleClient behavior ---


def test_client_uses_shell_false_argv_list(fixtures_path: Path) -> None:
    runner = ScriptedRunner([(0, _fixture(fixtures_path, "switch-list-multi.json"), "")])
    client = TailscaleClient(runner=runner)
    client.switch_list()
    assert runner.calls[0].argv == [DEFAULT_BINARY, "switch", "--list", "--json"]


def test_client_active_account(fixtures_path: Path) -> None:
    runner = ScriptedRunner([(0, _fixture(fixtures_path, "switch-list-multi.json"), "")])
    client = TailscaleClient(runner=runner)
    active = client.active_account()
    assert active == SwitchAccount(
        id="a1b2",
        nickname="user@github",
        tailnet="default.example",
        account="user@github",
        selected=True,
    )


def test_client_active_account_none_when_empty(fixtures_path: Path) -> None:
    runner = ScriptedRunner([(0, _fixture(fixtures_path, "switch-list-empty.json"), "")])
    client = TailscaleClient(runner=runner)
    assert client.active_account() is None


def test_client_switch_account_passes_id_as_argv(fixtures_path: Path) -> None:
    runner = ScriptedRunner([(0, "", "")])
    client = TailscaleClient(runner=runner)
    client.switch_account("c3d4")
    assert runner.calls[0].argv == [DEFAULT_BINARY, "switch", "c3d4"]


def test_client_switch_account_surfaces_stderr_on_failure() -> None:
    runner = ScriptedRunner([(1, "", "not signed in")])
    client = TailscaleClient(runner=runner)
    with pytest.raises(TailscaleError, match="not signed in"):
        client.switch_account("c3d4")


def test_client_set_exit_node_named() -> None:
    runner = ScriptedRunner([(0, "", "")])
    client = TailscaleClient(runner=runner)
    client.set_exit_node("beta-exit-node")
    assert runner.calls[0].argv == [DEFAULT_BINARY, "set", "--exit-node=beta-exit-node"]


def test_client_set_exit_node_clear_uses_empty_value() -> None:
    runner = ScriptedRunner([(0, "", "")])
    client = TailscaleClient(runner=runner)
    client.set_exit_node(None)
    assert runner.calls[0].argv == [DEFAULT_BINARY, "set", "--exit-node="]


def test_client_status_failure_surfaces_stderr() -> None:
    runner = ScriptedRunner([(1, "", "daemon not running")])
    client = TailscaleClient(runner=runner)
    with pytest.raises(TailscaleError, match="daemon not running"):
        client.status()


# --- convergence wait ---


def test_wait_for_backend_running_returns_quickly_when_already_ready(
    fixtures_path: Path,
) -> None:
    ready = _fixture(fixtures_path, "status-ready.json")
    runner = ScriptedRunner([(0, ready, "")])
    clock, sleep, sleeps = _fake_clock_and_sleep()
    client = TailscaleClient(runner=runner, sleep=sleep, clock=clock)
    status = client.wait_for_backend_running_and_tailnet(
        expected_tailnet="default.example",
        timeout_s=5.0,
    )
    assert status.backend_state == "Running"
    assert sleeps == []


def test_wait_polls_until_backend_state_running(fixtures_path: Path) -> None:
    starting = _fixture(fixtures_path, "status-starting.json")
    ready = _fixture(fixtures_path, "status-ready.json")
    runner = ScriptedRunner(
        [(0, starting, ""), (0, starting, ""), (0, ready, "")]
    )
    clock, sleep, sleeps = _fake_clock_and_sleep()
    client = TailscaleClient(runner=runner, sleep=sleep, clock=clock)
    client.wait_for_backend_running_and_tailnet(
        expected_tailnet="default.example",
        timeout_s=10.0,
        poll_interval_s=0.5,
    )
    assert sleeps == [0.5, 0.5]


def test_wait_polls_until_tailnet_matches(fixtures_path: Path) -> None:
    ready_default = _fixture(fixtures_path, "status-ready.json")
    ready_other = _fixture(fixtures_path, "status-no-exit-node.json")
    runner = ScriptedRunner(
        [(0, ready_default, ""), (0, ready_other, "")]
    )
    clock, sleep, sleeps = _fake_clock_and_sleep()
    client = TailscaleClient(runner=runner, sleep=sleep, clock=clock)
    status = client.wait_for_backend_running_and_tailnet(
        expected_tailnet="client-b.example",
        timeout_s=10.0,
        poll_interval_s=0.5,
    )
    assert status.current_tailnet_name == "client-b.example"
    assert sleeps == [0.5]


def test_wait_skips_tailnet_check_when_expected_is_empty(fixtures_path: Path) -> None:
    ready = _fixture(fixtures_path, "status-ready.json")
    runner = ScriptedRunner([(0, ready, "")])
    clock, sleep, _sleeps = _fake_clock_and_sleep()
    client = TailscaleClient(runner=runner, sleep=sleep, clock=clock)
    # tailnet="" → skip tailnet match; only BackendState == Running matters.
    status = client.wait_for_backend_running_and_tailnet(
        expected_tailnet="",
        timeout_s=5.0,
    )
    assert status.backend_state == "Running"


def test_wait_raises_convergence_timeout(fixtures_path: Path) -> None:
    starting = _fixture(fixtures_path, "status-starting.json")
    runner = ScriptedRunner([(0, starting, "")] * 100)  # never converges
    clock, sleep, _sleeps = _fake_clock_and_sleep()
    client = TailscaleClient(runner=runner, sleep=sleep, clock=clock)
    with pytest.raises(TailscaleConvergenceTimeout):
        client.wait_for_backend_running_and_tailnet(
            expected_tailnet="default.example",
            timeout_s=2.0,
            poll_interval_s=0.5,
        )


def test_wait_tolerates_transient_status_failures(fixtures_path: Path) -> None:
    ready = _fixture(fixtures_path, "status-ready.json")
    runner = ScriptedRunner(
        [
            (1, "", "transient"),
            (1, "", "transient"),
            (0, ready, ""),
        ]
    )
    clock, sleep, _sleeps = _fake_clock_and_sleep()
    client = TailscaleClient(runner=runner, sleep=sleep, clock=clock)
    status = client.wait_for_backend_running_and_tailnet(
        expected_tailnet="default.example",
        timeout_s=10.0,
        poll_interval_s=0.5,
    )
    assert status.backend_state == "Running"


def test_subprocess_timeout_is_passed_through() -> None:
    runner = ScriptedRunner([(0, "[]", "")])
    client = TailscaleClient(runner=runner)
    client.switch_list()
    assert runner.calls[0].timeout is not None
    assert runner.calls[0].timeout > 0
