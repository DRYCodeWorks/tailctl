"""Profile data type and lookup table.

Profiles are the user's logical name → ``(account_id, tailnet, exit_node)``
mapping. ``config.py`` (next PR up) constructs ``Profiles`` from
``profiles.yaml``; the coordinator only depends on this lightweight type.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PortForward:
    """A native-TCP forward for a profile.

    Under the userspace-per-identity model, a profile's tailnet may host
    services (ClickHouse, Postgres, …) that native clients reach by connecting
    to a stable ``127.0.0.1:<local_port>``. The forwarder relays that local
    port through the profile's userspace tailscaled to ``remote_host:remote_port``.
    ``local_port`` is optional; when 0/None the instance allocates a free port.
    The resolved port is exported to ``tailctl run`` subprocesses as
    ``<SERVICE>_ADDR=127.0.0.1:<local_port>`` (service upper-cased).
    """

    service: str
    remote_host: str
    remote_port: int
    local_port: int = 0


@dataclass(frozen=True)
class Profile:
    name: str
    account_id: str
    tailnet: str | None = None
    exit_node: str | None = None
    expected_egress_ip: str | None = None
    # Userspace-model fields:
    accept_routes: bool = True
    port_forwards: tuple[PortForward, ...] = field(default_factory=tuple)
    # Name of an env var holding a Tailscale auth key for this profile's
    # tailnet (sourced from BWS). When set and present in the environment,
    # first login is non-interactive (no browser URL). Absent → fall back to
    # the interactive one-time login.
    auth_key_env: str | None = None


class UnknownProfileError(KeyError):
    """Profile name does not exist in profiles.yaml."""


@dataclass(frozen=True)
class Profiles:
    """Read-only profile table with a configured default."""

    default_name: str
    by_name: dict[str, Profile]

    def get(self, name: str) -> Profile:
        try:
            return self.by_name[name]
        except KeyError as exc:
            raise UnknownProfileError(name) from exc

    @property
    def default(self) -> Profile:
        return self.get(self.default_name)

    def names(self) -> list[str]:
        return sorted(self.by_name.keys())
