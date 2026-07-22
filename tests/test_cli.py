"""Top-level CLI behavior tests.

These exercise the end-to-end argparse paths against monkeypatched
TailscaleClient and StateStore. The real subprocess never runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tailctl import cli, paths


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TAILCTL_HOME", str(tmp_path))
    return tmp_path


def _write_minimal_config(home: Path) -> None:
    paths.profiles_yaml().write_text(
        """
default: dg
profiles:
  dg:
    account_id: a1b2
    tailnet: drycode.github
  beta:
    account_id: c3d4
    tailnet: beta.example.ts.net
    exit_node: beta-exit
"""
    )


# --- init ---


def test_init_refuses_if_no_accounts_signed_in(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Round-2 review fix: zero signed-in accounts must be a clear refusal,
    not a silent invalid scaffold."""
    # Pretend tailscale binary exists.
    monkeypatch.setattr(Path, "exists", lambda self: True)

    class FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None: ...

        def switch_list(self) -> list[Any]:
            return []

    monkeypatch.setattr("tailctl.cli.TailscaleClient", FakeClient)
    rc = cli.main(["init"])
    captured = capsys.readouterr()
    assert rc != 0
    assert "no Tailscale accounts signed in" in captured.err


def test_init_writes_yaml_from_switch_list(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "exists", lambda self: True)

    from tailctl.tailscale import SwitchAccount

    accounts = [
        SwitchAccount(
            id="a1b2", nickname="dg", tailnet="drycode.github",
            account="me@github", selected=True,
        ),
        SwitchAccount(
            id="c3d4", nickname="beta", tailnet="beta.example.ts.net",
            account="me@github", selected=False,
        ),
    ]

    class FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None: ...

        def switch_list(self) -> list[Any]:
            return accounts

    monkeypatch.setattr("tailctl.cli.TailscaleClient", FakeClient)
    # Force is needed because the broad `Path.exists` monkeypatch above also
    # makes the "config already exists" guard fire; real users without an
    # existing file don't need --force.
    rc = cli.main(["init", "--force"])
    assert rc == 0
    assert paths.profiles_yaml().exists()
    # Loaded config should parse cleanly.
    from tailctl.config import load
    config = load()
    assert "drycode.github" in config.profiles.by_name
    assert config.profiles.default_name == "drycode.github"


def test_init_refuses_to_overwrite_without_force(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.ensure_home()
    paths.profiles_yaml().write_text("existing: true\n")
    monkeypatch.setattr(Path, "exists", lambda self: True)

    from tailctl.tailscale import SwitchAccount

    class FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None: ...

        def switch_list(self) -> list[Any]:
            return [SwitchAccount(id="x", nickname="x", tailnet="x", account="x", selected=True)]

    monkeypatch.setattr("tailctl.cli.TailscaleClient", FakeClient)
    rc = cli.main(["init"])
    captured = capsys.readouterr()
    assert rc != 0
    assert "--force" in captured.err


# --- doctor ---


def test_doctor_reports_missing_config(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["doctor"])
    assert rc != 0
    assert "tailctl init" in capsys.readouterr().err


def test_doctor_reports_missing_tailscaled(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """New-model doctor fails when the userspace tailscaled binary is absent."""
    paths.profiles_yaml().write_text(
        "default: dg\n"
        "tailscaled_binary: /nonexistent/tailscaled\n"
        "profiles:\n"
        "  dg:\n"
        "    account_id: a1b2\n"
        "    tailnet: drycode.github\n"
    )
    rc = cli.main(["doctor"])
    out = capsys.readouterr()
    assert rc != 0
    assert "tailscaled: NOT FOUND" in out.err


def test_doctor_happy_lists_profiles(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Binaries present → ok, with a per-profile summary."""
    _write_minimal_config(home)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tailscaled:" in out
    assert "dg:" in out and "beta:" in out


# --- profiles / reset ---


def test_ps_empty_when_no_instances(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_minimal_config(home)
    rc = cli.main(["ps"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no running instances" in out


def test_reap_command_runs_on_empty_registry(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_minimal_config(home)
    rc = cli.main(["reap"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "reaped 0 instance(s)" in out


def test_config_rejects_dotted_auth_key_env(home: Path) -> None:
    """auth_key_env must be a valid env var name (no '.'/'-')."""
    paths.profiles_yaml().write_text(
        "default: dg\nprofiles:\n  dg:\n    account_id: a1b2\n"
        "    auth_key_env: my-tailnet.key\n"
    )
    rc = cli.main(["profiles"])
    assert rc != 0  # config load fails with a clear error


def test_profiles_lists_with_default_marker(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_minimal_config(home)
    rc = cli.main(["profiles"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dg" in out
    assert "(default)" in out
    assert "beta" in out


def test_reset_refuses_without_force(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_minimal_config(home)
    rc = cli.main(["reset"])
    captured = capsys.readouterr()
    assert rc != 0
    assert "--force" in captured.err


def test_reset_force_with_no_instances_ok(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_minimal_config(home)
    rc = cli.main(["reset", "--force"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "stopped 0 instance(s)" in captured.out


# --- bootstrap ---


def test_bootstrap_appends_profile_preserving_existing(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_minimal_config(home)
    rc = cli.main(
        [
            "bootstrap", "newnet",
            "--account-id", "ab12",
            "--tailnet", "new.example",
            "--exit-node", "ex1",
            "--port-forward", "ch=10.0.0.5:9000",
        ]
    )
    assert rc == 0
    # Existing profiles untouched; new one parses with all fields.
    from tailctl.config import load as load_config

    cfg = load_config()
    assert "dg" in cfg.profiles.by_name and "beta" in cfg.profiles.by_name
    p = cfg.profiles.get("newnet")
    assert p.account_id == "ab12"
    assert p.exit_node == "ex1"
    assert p.auth_key_env == "newnet_ts_authkey"  # derived default
    assert p.port_forwards[0].service == "ch"
    assert p.port_forwards[0].remote_host == "10.0.0.5"
    assert p.port_forwards[0].remote_port == 9000
    out = capsys.readouterr().out
    assert "mint a REUSABLE auth key" in out  # next-steps guidance


def test_bootstrap_refuses_duplicate_without_force(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_minimal_config(home)
    rc = cli.main(["bootstrap", "beta", "--account-id", "c3d4", "--tailnet", "beta.example.ts.net"])
    assert rc != 0
    assert "already exists" in capsys.readouterr().err


def test_bootstrap_requires_account_or_tailnet(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_minimal_config(home)
    rc = cli.main(["bootstrap", "newnet"])
    assert rc != 0
    assert "--account-id or --tailnet" in capsys.readouterr().err
