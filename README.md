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

Prototype. Design plan: `~/.claude/plans/misty-dazzling-pebble.md`.

## Quick view

```
tailctl init                       # scaffold ~/.tailctl/profiles.yaml
tailctl doctor                     # validate config + probe daemon + verify accounts
tailctl bootstrap <name> --tailnet <t> [--exit-node <e>] [--port-forward svc=host:port]
                                   # onboard a NEW tailnet: discovers account_id,
                                   #   appends a profile (preserving comments), prints
                                   #   the key steps (mint in console -> store in BWS)
tailctl up acme-dev              # spawn a userspace tailscaled for the profile
                                   #   (first time: prints a one-time auth URL)
tailctl run acme-dev -- curl http://service.internal   # route a command via that identity
tailctl run acme-dev -- clickhouse-client --host 127.0.0.1 --port $CLICKHOUSE_ADDR
tailctl ps                         # list running instances + their proxy ports/forwards
tailctl down acme-dev            # release (refcount--); stops the daemon at zero
```

`run` sets `ALL_PROXY`/`HTTPS_PROXY`/`HTTP_PROXY` (+ `NO_PROXY`) so proxy-aware
clients route through the identity, and exports `<SERVICE>_ADDR=127.0.0.1:<port>`
for each configured `port_forward` so native-TCP clients connect to localhost.

## profiles.yaml

```yaml
default: drycode-github
tailscaled_binary: /opt/homebrew/opt/tailscale/bin/tailscaled
profiles:
  acme-dev:
    account_id: a2c4
    tailnet: acme.example.ts.net
    exit_node: tailscale-subnet-router-development
    accept_routes: true
    auth_key_env: ACME_TS_AUTHKEY   # optional: env var holding a Tailscale
                                      # auth key (from BWS) for non-interactive
                                      # first login — no browser URL
    port_forwards:
      - service: clickhouse        # exported as CLICKHOUSE_ADDR
        remote_host: 100.64.0.10
        remote_port: 9000
```

## Authentication

Each profile is a distinct device on its tailnet, authenticated **once** — the
login persists in the profile's statedir and is reused on every later `up`/`run`
(teardown never logs out). Two ways to do that one-time login:

- **Auth key (recommended):** set `auth_key_env` to an env var holding a
  Tailscale auth key (minted in the tailnet's admin console, stored in BWS).
  First `up` logs in non-interactively — no browser.
- **Interactive:** with no auth key, the first `up` prints a login URL to open
  once.

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
