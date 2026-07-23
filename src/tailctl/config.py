"""profiles.yaml loader and validator.

The on-disk shape:

    default: drycode-github
    tailscale_binary: /Applications/Tailscale.app/Contents/MacOS/Tailscale  # optional
    profiles:
      drycode-github:
        account_id: a1b2
        tailnet: drycode.github            # optional; empty/null skips tailnet verification
        exit_node: null                    # optional
        expected_egress_ip: null           # optional; only checked with --verify-egress
        description: ""                    # optional, informational only
      ...

Every identifier (profile name, account_id, exit_node) must match
``^[A-Za-z0-9._-]+$`` so it can flow through subprocess argv without
quoting concerns. Duplicate account_ids are rejected. ``default`` must
reference an existing profile name.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tailctl import paths
from tailctl.profiles import PortForward, Profile, Profiles
from tailctl.tailscale import DEFAULT_BINARY

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Env-var-name safe: what a shell can actually export/expand. Used for
# port-forward service names (exported as <SERVICE>_ADDR) and auth_key_env.
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def _resolve_homebrew_binary(name: str, fallback: str) -> str:
    """Locate a Homebrew-installed Tailscale binary.

    PATH first, so this works on both the arm64 (/opt/homebrew) and Intel
    (/usr/local) prefixes without hardcoding either. Fall back to the known
    keg paths for thin-PATH contexts (launchd agents don't get the Homebrew
    bin dir), then to ``fallback`` so `doctor` can report a concrete path.
    """
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew", "/usr/local"):
        candidate = f"{prefix}/opt/tailscale/bin/{name}"
        if Path(candidate).exists():
            return candidate
    return fallback


# Userspace-model defaults. Resolved once at import; profiles.yaml overrides win.
DEFAULT_TAILSCALED = _resolve_homebrew_binary(
    "tailscaled", "/opt/homebrew/opt/tailscale/bin/tailscaled"
)
# The tailscale CLI used to control a per-identity daemon over its --socket.
# Distinct from `tailscale_binary` (the GUI app's CLI, used only for account
# discovery in init/bootstrap).
DEFAULT_TAILSCALE_CLI = _resolve_homebrew_binary("tailscale", "/opt/homebrew/bin/tailscale")


class ConfigError(RuntimeError):
    """profiles.yaml is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class Config:
    """Top-level parsed configuration."""

    profiles: Profiles
    tailscale_binary: str
    # Userspace-model config: the headless daemon binary and the CLI used to
    # drive a per-identity daemon over its --socket.
    tailscaled_binary: str = DEFAULT_TAILSCALED
    tailscale_cli_binary: str = DEFAULT_TAILSCALE_CLI


def load(path: Path | None = None) -> Config:
    """Load + validate profiles.yaml. Returns a Config on success.

    Raises ``ConfigError`` with a specific message on any problem.
    """
    target = path or paths.profiles_yaml()
    if not target.exists():
        raise ConfigError(
            f"profiles.yaml not found at {target}. "
            f"Run `tailctl init` to scaffold one."
        )
    try:
        raw = yaml.safe_load(target.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"profiles.yaml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("profiles.yaml: top-level must be a mapping")
    return _parse(raw)


def _parse(raw: dict[str, Any]) -> Config:
    default_name = raw.get("default")
    if not isinstance(default_name, str) or not default_name:
        raise ConfigError("profiles.yaml: `default` is required and must be a string")
    if not _ID_RE.match(default_name):
        raise ConfigError(
            f"profiles.yaml: `default` value {default_name!r} contains "
            f"characters outside [A-Za-z0-9._-]"
        )

    binary = raw.get("tailscale_binary", DEFAULT_BINARY)
    if not isinstance(binary, str) or not binary:
        raise ConfigError("profiles.yaml: `tailscale_binary` must be a non-empty string")

    tailscaled_binary = raw.get("tailscaled_binary", DEFAULT_TAILSCALED)
    if not isinstance(tailscaled_binary, str) or not tailscaled_binary:
        raise ConfigError("profiles.yaml: `tailscaled_binary` must be a non-empty string")

    tailscale_cli_binary = raw.get("tailscale_cli_binary", DEFAULT_TAILSCALE_CLI)
    if not isinstance(tailscale_cli_binary, str) or not tailscale_cli_binary:
        raise ConfigError("profiles.yaml: `tailscale_cli_binary` must be a non-empty string")

    profiles_section = raw.get("profiles")
    if not isinstance(profiles_section, dict) or not profiles_section:
        raise ConfigError("profiles.yaml: `profiles` must be a non-empty mapping")

    by_name: dict[str, Profile] = {}
    # Uniqueness key is (account_id, exit_node), not account_id alone.
    # Many clients use one tailnet account but distinct exit nodes per
    # environment (dev / staging / prod) — those are legitimate distinct
    # profiles. The narrower check still catches the "I typed the same
    # profile twice" typo.
    seen_account_exit: dict[tuple[str, str | None], str] = {}
    for name, body in profiles_section.items():
        if not isinstance(name, str):
            raise ConfigError(f"profiles.yaml: profile name must be a string, got {name!r}")
        if not _ID_RE.match(name):
            raise ConfigError(
                f"profiles.yaml: profile name {name!r} contains characters "
                f"outside [A-Za-z0-9._-]"
            )
        if not isinstance(body, dict):
            raise ConfigError(
                f"profiles.yaml: profile {name!r} must be a mapping"
            )
        account_id = body.get("account_id")
        if not isinstance(account_id, str) or not account_id:
            raise ConfigError(
                f"profiles.yaml: profile {name!r} missing required `account_id`"
            )
        if not _ID_RE.match(account_id):
            raise ConfigError(
                f"profiles.yaml: profile {name!r} `account_id` {account_id!r} "
                f"contains characters outside [A-Za-z0-9._-]"
            )
        # Defer the uniqueness check until we've parsed exit_node below.

        tailnet = body.get("tailnet") or None
        if tailnet is not None and not isinstance(tailnet, str):
            raise ConfigError(
                f"profiles.yaml: profile {name!r} `tailnet` must be a string or null"
            )

        exit_node = body.get("exit_node") or None
        if exit_node is not None:
            if not isinstance(exit_node, str):
                raise ConfigError(
                    f"profiles.yaml: profile {name!r} `exit_node` must be a string or null"
                )
            if not _ID_RE.match(exit_node):
                raise ConfigError(
                    f"profiles.yaml: profile {name!r} `exit_node` {exit_node!r} "
                    f"contains characters outside [A-Za-z0-9._-]"
                )

        expected_egress_ip = body.get("expected_egress_ip") or None
        if expected_egress_ip is not None and not isinstance(expected_egress_ip, str):
            raise ConfigError(
                f"profiles.yaml: profile {name!r} `expected_egress_ip` "
                f"must be a string or null"
            )

        uniq_key = (account_id, exit_node)
        if uniq_key in seen_account_exit:
            raise ConfigError(
                f"profiles.yaml: (account_id={account_id!r}, exit_node="
                f"{exit_node!r}) is duplicated by both "
                f"{seen_account_exit[uniq_key]!r} and {name!r}"
            )
        seen_account_exit[uniq_key] = name

        accept_routes = body.get("accept_routes", True)
        if not isinstance(accept_routes, bool):
            raise ConfigError(
                f"profiles.yaml: profile {name!r} `accept_routes` must be a boolean"
            )

        auth_key_env = body.get("auth_key_env") or None
        if auth_key_env is not None and (
            not isinstance(auth_key_env, str) or not _ENV_RE.match(auth_key_env)
        ):
            raise ConfigError(
                f"profiles.yaml: profile {name!r} `auth_key_env` must be a valid "
                f"env var name [A-Za-z_][A-Za-z0-9_]* (got {auth_key_env!r}); a "
                f"shell cannot export names containing '.' or '-'."
            )

        port_forwards = _parse_port_forwards(name, body.get("port_forwards"))

        by_name[name] = Profile(
            name=name,
            account_id=account_id,
            tailnet=tailnet,
            exit_node=exit_node,
            expected_egress_ip=expected_egress_ip,
            accept_routes=accept_routes,
            port_forwards=port_forwards,
            auth_key_env=auth_key_env,
        )

    if default_name not in by_name:
        raise ConfigError(
            f"profiles.yaml: `default` value {default_name!r} does not match "
            f"any profile name (have {sorted(by_name.keys())!r})"
        )

    return Config(
        profiles=Profiles(default_name=default_name, by_name=by_name),
        tailscale_binary=binary,
        tailscaled_binary=tailscaled_binary,
        tailscale_cli_binary=tailscale_cli_binary,
    )


def _parse_port_forwards(profile_name: str, raw: Any) -> tuple[PortForward, ...]:
    """Parse a profile's optional ``port_forwards`` list into PortForward records."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(
            f"profiles.yaml: profile {profile_name!r} `port_forwards` must be a list"
        )
    out: list[PortForward] = []
    seen_services: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"profiles.yaml: profile {profile_name!r} port_forwards[{i}] "
                f"must be a mapping"
            )
        service = entry.get("service")
        # Must be env-var-safe: it's exported to `run` subprocesses as
        # <SERVICE>_ADDR, so '.'/'-' would produce a name no shell can expand.
        if not isinstance(service, str) or not _ENV_RE.match(service):
            raise ConfigError(
                f"profiles.yaml: profile {profile_name!r} port_forwards[{i}] "
                f"`service` must be a valid env var name [A-Za-z_][A-Za-z0-9_]* "
                f"(exported as <SERVICE>_ADDR); got {service!r}"
            )
        if service in seen_services:
            raise ConfigError(
                f"profiles.yaml: profile {profile_name!r} has duplicate "
                f"port_forward service {service!r}"
            )
        seen_services.add(service)
        remote_host = entry.get("remote_host")
        if not isinstance(remote_host, str) or not remote_host:
            raise ConfigError(
                f"profiles.yaml: profile {profile_name!r} port_forwards[{i}] "
                f"missing `remote_host`"
            )
        remote_port = entry.get("remote_port")
        if not isinstance(remote_port, int) or not (1 <= remote_port <= 65535):
            raise ConfigError(
                f"profiles.yaml: profile {profile_name!r} port_forwards[{i}] "
                f"`remote_port` must be an int in [1, 65535]"
            )
        local_port = entry.get("local_port", 0)
        if not isinstance(local_port, int) or not (0 <= local_port <= 65535):
            raise ConfigError(
                f"profiles.yaml: profile {profile_name!r} port_forwards[{i}] "
                f"`local_port` must be an int in [0, 65535] (0 = auto)"
            )
        out.append(
            PortForward(
                service=service,
                remote_host=remote_host,
                remote_port=remote_port,
                local_port=local_port,
            )
        )
    return tuple(out)
