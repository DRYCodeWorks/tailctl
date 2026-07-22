"""profiles.yaml loader + validation tests.

These pin the load-bearing invariants of the security-conscious config:

- profile names, account_ids, and exit_node values must match
  ^[A-Za-z0-9._-]+$ so they can safely flow into subprocess argv.
- ``default`` must reference an existing profile.
- account_ids must be unique across profiles.
- Missing top-level keys produce specific error messages, not crashes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tailctl import paths
from tailctl.config import ConfigError, load


def _write_profiles(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "profiles.yaml"
    p.write_text(text)
    return p


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TAILCTL_HOME", str(tmp_path))
    return tmp_path


def test_load_missing_file_message_mentions_init(home: Path) -> None:
    with pytest.raises(ConfigError, match="`tailctl init`"):
        load()


def test_load_invalid_yaml(home: Path) -> None:
    paths.profiles_yaml().write_text("not: valid: yaml: [")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load()


def test_load_non_mapping_top_level(home: Path) -> None:
    paths.profiles_yaml().write_text("- a\n- b")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load()


def test_load_happy_path(home: Path) -> None:
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
    expected_egress_ip: 203.0.113.10
"""
    )
    config = load()
    assert config.profiles.default_name == "dg"
    assert config.profiles.get("beta").exit_node == "beta-exit"
    assert config.profiles.get("beta").expected_egress_ip == "203.0.113.10"
    assert config.profiles.get("dg").exit_node is None


def test_load_rejects_missing_default(home: Path) -> None:
    paths.profiles_yaml().write_text(
        """
profiles:
  a:
    account_id: 1234
"""
    )
    with pytest.raises(ConfigError, match="`default` is required"):
        load()


def test_load_rejects_default_pointing_at_unknown_profile(home: Path) -> None:
    paths.profiles_yaml().write_text(
        """
default: ghost
profiles:
  a:
    account_id: "1234"
"""
    )
    with pytest.raises(ConfigError, match="does not match any profile"):
        load()


def test_load_rejects_missing_profiles(home: Path) -> None:
    paths.profiles_yaml().write_text("default: dg\n")
    with pytest.raises(ConfigError, match="`profiles`"):
        load()


def test_load_rejects_empty_profiles(home: Path) -> None:
    paths.profiles_yaml().write_text("default: dg\nprofiles: {}\n")
    with pytest.raises(ConfigError, match="non-empty mapping"):
        load()


def test_load_rejects_duplicate_account_id_AND_exit_node(home: Path) -> None:
    """Uniqueness key is the (account_id, exit_node) tuple — catches the
    "same profile typed twice" case."""
    paths.profiles_yaml().write_text(
        """
default: a
profiles:
  a:
    account_id: same
    exit_node: my-exit
  b:
    account_id: same
    exit_node: my-exit
"""
    )
    with pytest.raises(ConfigError, match="duplicated by both"):
        load()


def test_load_allows_same_account_id_with_different_exit_nodes(home: Path) -> None:
    """Common case: one tailnet account, multiple env-specific exit nodes
    (e.g. acme-dev / acme-staging / acme-prod all share one Acme
    Tailscale account but route through different subnet routers).
    """
    paths.profiles_yaml().write_text(
        """
default: dev
profiles:
  dev:
    account_id: shared
    exit_node: env-dev
  staging:
    account_id: shared
    exit_node: env-staging
  prod:
    account_id: shared
    exit_node: env-prod
"""
    )
    config = load()
    assert {p.exit_node for p in config.profiles.by_name.values()} == {
        "env-dev",
        "env-staging",
        "env-prod",
    }


def test_load_rejects_same_account_id_with_both_exit_nodes_null(home: Path) -> None:
    """Same account + both exit_node null is still a duplicate."""
    paths.profiles_yaml().write_text(
        """
default: a
profiles:
  a:
    account_id: same
  b:
    account_id: same
"""
    )
    with pytest.raises(ConfigError, match="duplicated by both"):
        load()


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile-name", "has space"),
        ("profile-name", "weird/slash"),
        ("account_id", "has space"),
        ("account_id", "dollar$sign"),
        ("exit_node", "with space"),
    ],
)
def test_load_rejects_unsafe_identifier_chars(
    home: Path, field: str, value: str
) -> None:
    if field == "profile-name":
        text = f"""
default: ok
profiles:
  ok:
    account_id: aaaa
  "{value}":
    account_id: bbbb
"""
    elif field == "account_id":
        text = f"""
default: ok
profiles:
  ok:
    account_id: "{value}"
"""
    else:  # exit_node
        text = f"""
default: ok
profiles:
  ok:
    account_id: aaaa
    exit_node: "{value}"
"""
    paths.profiles_yaml().write_text(text)
    with pytest.raises(ConfigError, match=r"\[A-Za-z0-9\._-\]"):
        load()


def test_load_missing_account_id_message(home: Path) -> None:
    paths.profiles_yaml().write_text(
        """
default: a
profiles:
  a: {}
"""
    )
    with pytest.raises(ConfigError, match="missing required `account_id`"):
        load()


def test_load_uses_default_tailscale_binary(home: Path) -> None:
    paths.profiles_yaml().write_text(
        """
default: a
profiles:
  a:
    account_id: aaaa
"""
    )
    from tailctl.tailscale import DEFAULT_BINARY

    config = load()
    assert config.tailscale_binary == DEFAULT_BINARY


def test_load_allows_custom_tailscale_binary(home: Path) -> None:
    paths.profiles_yaml().write_text(
        """
default: a
tailscale_binary: /opt/custom/tailscale
profiles:
  a:
    account_id: aaaa
"""
    )
    config = load()
    assert config.tailscale_binary == "/opt/custom/tailscale"


def test_load_empty_tailscale_binary_rejected(home: Path) -> None:
    paths.profiles_yaml().write_text(
        """
default: a
tailscale_binary: ""
profiles:
  a:
    account_id: aaaa
"""
    )
    with pytest.raises(ConfigError, match="tailscale_binary"):
        load()
