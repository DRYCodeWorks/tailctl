<p align="center">
  <img src="https://raw.githubusercontent.com/DRYCodeWorks/tailctl/main/docs/tailctl-banner.jpg" alt="tailctl — per-identity Tailscale networking for parallel sessions on a single Mac" width="100%">
</p>

# tailctl

Per-identity Tailscale networking for parallel sessions on a single Mac.

`tailctl` gives each process (e.g. a Claude Code session in its own Ghostty window)
its **own** Tailscale identity — a private, headless userspace `tailscaled` with a
per-identity SOCKS5/HTTP proxy and localhost port-forwards — instead of switching
the machine-global Tailscale account. Many identities run at once, and the GUI
Tailscale app (the browser's default tailnet) is never touched. No FIFO queue, no
shared network, no flapping.

Agent traffic is opted in per command via proxy env vars (`tailctl run`) or stable
localhost forwards for native-TCP clients (ClickHouse, Postgres, …).

No always-on daemon beyond the per-identity tailscaled instances. Single-laptop scope.

## Status

Working prototype — macOS-only, single-laptop scope.

## Install

Requires macOS, Python 3.11+, and the Homebrew Tailscale daemon (see
[Requirements](#requirements)).

```
brew install drycodeworks/tap/tailctl
```

Homebrew pulls in the `tailscale` formula for you. From PyPI instead, install
the daemon yourself:

```
brew install tailscale
pipx install tailctl        # or: pip install tailctl
```

Or from source:

```
brew install tailscale
git clone https://github.com/DRYCodeWorks/tailctl && cd tailctl
python3 -m venv .venv && .venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/tailctl" ~/.local/bin/tailctl   # put it on PATH
```

## Quick view

```
tailctl init                       # scaffold ~/.tailctl/profiles.yaml
tailctl doctor                     # validate config + probe daemon + verify accounts
tailctl bootstrap <name> --tailnet <t> [--exit-node <e>] \
  [--auth-key-env ENV] [--auth-key-ssm NAME [--auth-key-ssm-profile P] [--auth-key-ssm-region R]] \
  [--port-forward svc=host:port]   # onboard a new tailnet with explicit auth sources
tailctl up acme-dev              # spawn a userspace tailscaled for the profile
                                   #   (first time: prints a one-time auth URL)
tailctl run acme-dev -- curl http://service.internal   # route a command via that identity
tailctl run acme-dev -- clickhouse-client --host 127.0.0.1 --port $CLICKHOUSE_ADDR
tailctl ps                         # list running instances + their proxy ports/forwards
tailctl shields [--fix]            # confirm every instance refuses inbound (exit 5 if not)
tailctl down acme-dev            # release (refcount--); stops the daemon at zero
```

A userspace node delivers inbound tailnet connections to this host's `127.0.0.1`,
so tailctl keeps shields up on every instance: `up` and `run` read the pref back
and raise it, and refuse to hand out an instance they can't confirm.

`run` sets `ALL_PROXY`/`HTTPS_PROXY`/`HTTP_PROXY` (+ `NO_PROXY`) so proxy-aware
clients route through the identity, and exports `<SERVICE>_ADDR=<listen_host>:<port>`
for each configured `port_forward` so native-TCP clients connect to localhost.

The proxies take no credentials, so anything that can reach their port can use
the identity. They bind `listen_host` (default `127.0.0.1`). On a machine that
runs containers, set it to a loopback alias: OrbStack forwards
`host.docker.internal` to `127.0.0.1` and `::1` (but not to other `lo0`
addresses), so every container could otherwise tunnel into the tailnet. The
alias must already be on `lo0` (`sudo ifconfig lo0 alias 127.84.67.1
255.255.255.255`, run at boot). If it is missing, `up` and `run` refuse to
start rather than fall back to `127.0.0.1`.
An instance spawned before `listen_host` changed keeps its old address until
its holders exit, and every claim on it prints a warning.

Port forwards are for protocols that are not HTTP (SQL Server, ClickHouse native,
Postgres). A forward relays raw bytes, so a web page that rebinds a hostname to
its address could talk through it. Reach HTTP services through `run`'s proxy
variables instead.

## profiles.yaml

```yaml
default: acme-dev
tailscaled_binary: /opt/homebrew/opt/tailscale/bin/tailscaled
listen_host: 127.84.67.1         # optional loopback alias; default 127.0.0.1
profiles:
  acme-dev:
    account_id: a2c4
    tailnet: acme.example.ts.net
    exit_node: tailscale-subnet-router-development
    accept_routes: true
    auth_key_env: ACME_TS_AUTHKEY   # optional headless-wrapper short circuit
    auth_key_ssm: /acme/tailscale/dev_authkey  # optional SSM fallback
    auth_key_ssm_profile: acme-secrets         # optional AWS CLI profile for that read
    auth_key_ssm_region: us-west-2             # optional; default is the profile's region
    port_forwards:
      - service: clickhouse        # exported as CLICKHOUSE_ADDR
        remote_host: 100.64.0.10
        remote_port: 9000
```

## Authentication

Each profile is a distinct device on its tailnet, authenticated **once**—the
login persists in the profile's statedir and is reused on every later `up`/`run`
(teardown never logs out). Authentication order:

1. `auth_key_env`, when a headless wrapper injects it.
2. The `auth_key_ssm` parameter, read with the AWS CLI as
   `auth_key_ssm_profile` in `auth_key_ssm_region`. Without those, the read uses
   your default AWS profile and its configured region.
3. Interactive login URL when neither source resolves.

Both sources are optional and independent. The environment always wins, so a
headless wrapper that injects the key never attempts an SSO-backed read. SSM
subprocesses receive no inherited `AWS_*` variables, so an ambient profile,
credential or endpoint override can't redirect the read.

Profiles that share an account but differ by exit node (e.g. Acme
dev/staging/prod) are separate profiles → separate nodes → one auth each.

## Requirements

- macOS
- Homebrew Tailscale for the userspace daemon: `brew install tailscale`
  (coexists with the GUI Tailscale.app, which keeps the browser's default tailnet)
- Python 3.11+
- Each profile's Tailscale account authenticated once per profile on first `up`

## License

MIT
