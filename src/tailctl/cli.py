"""tailctl command-line entry point.

Per-identity userspace model: each profile a session needs runs its own
headless userspace tailscaled (SOCKS5 + HTTP proxy, localhost port-forwards),
fully isolated from the machine-global Tailscale identity the browser uses.

Subcommands:
  init     — scaffold ~/.tailctl/profiles.yaml from `tailscale switch --list --json`
  doctor   — validate config, probe daemon, verify accounts are signed in
  profiles — list configured profiles
  up       — ensure a profile's userspace tailscaled is running (refcount++)
  run      — run a command routed through a profile's identity
  down     — release a profile (refcount--); stop the daemon at zero
  ps       — list running userspace instances
  reset    — stop ALL instances and clear the registry (requires --force)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import psutil
import yaml

from tailctl import paths
from tailctl.config import Config, ConfigError
from tailctl.config import load as load_config
from tailctl.instance_manager import InstanceError, InstanceManager
from tailctl.profiles import UnknownProfileError
from tailctl.registry import RegistryStore
from tailctl.tailscale import (
    DEFAULT_BINARY,
    TailscaleClient,
    TailscaleError,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 124
EXIT_BROKEN = 3
EXIT_CORRUPT = 4
EXIT_DRIFT = 5
EXIT_AUTH = 7


# === entry points ========================================================


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = args.func  # set by subparser default
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tailctl",
        description="Tailnet identity coordinator for parallel sessions on a single Mac.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="scaffold profiles.yaml")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing profiles.yaml",
    )
    p_init.set_defaults(func=_cmd_init)

    p_doctor = sub.add_parser("doctor", help="validate config and probe Tailscale daemon")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_profiles = sub.add_parser("profiles", help="list configured profiles")
    p_profiles.set_defaults(func=_cmd_profiles)

    p_reset = sub.add_parser("reset", help="stop ALL instances and clear the registry")
    p_reset.add_argument("--force", action="store_true", help="required")
    p_reset.set_defaults(func=_cmd_reset)

    # --- userspace-per-identity verbs ---
    p_up = sub.add_parser("up", help="ensure a userspace tailscaled is running for a profile")
    p_up.add_argument("profile", help="profile name from profiles.yaml")
    p_up.set_defaults(func=_cmd_up)

    p_down = sub.add_parser("down", help="release/stop a profile's userspace instance")
    p_down.add_argument("profile", help="profile name from profiles.yaml")
    p_down.set_defaults(func=_cmd_down)

    p_run = sub.add_parser(
        "run", help="run a command routed through a profile's identity"
    )
    p_run.add_argument("profile", help="profile name from profiles.yaml")
    p_run.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="-- <command> [args...] to run with proxy env set",
    )
    p_run.set_defaults(func=_cmd_run)

    p_ps = sub.add_parser("ps", help="list running userspace instances")
    p_ps.add_argument("--json", action="store_true", help="emit JSON")
    p_ps.set_defaults(func=_cmd_ps)

    p_reap = sub.add_parser(
        "reap", help="stop instances whose owner died or whose linger expired"
    )
    p_reap.set_defaults(func=_cmd_reap)

    p_boot = sub.add_parser(
        "bootstrap", help="onboard a new tailnet: add a profile to profiles.yaml"
    )
    p_boot.add_argument("name", help="new profile name")
    p_boot.add_argument("--account-id", help="Tailscale account id (else discover via --tailnet)")
    p_boot.add_argument(
        "--tailnet", help="tailnet name; discovers account_id from signed-in accounts"
    )
    p_boot.add_argument("--exit-node", help="exit-node hostname (optional)")
    p_boot.add_argument(
        "--auth-key-env",
        help="env/BWS secret name holding this tailnet's auth key "
        "(default: <name-with-underscores>_ts_authkey)",
    )
    p_boot.add_argument(
        "--port-forward",
        action="append",
        default=[],
        metavar="SVC=HOST:PORT",
        help="native-TCP forward, repeatable (e.g. clickhouse=100.64.0.10:9000)",
    )
    p_boot.add_argument(
        "--accept-routes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="accept advertised subnet routes (default: yes)",
    )
    p_boot.add_argument(
        "--register",
        action="store_true",
        help="after writing, run `up` to register the node now (needs the key)",
    )
    p_boot.add_argument("--force", action="store_true", help="overwrite an existing profile")
    p_boot.set_defaults(func=_cmd_bootstrap)

    return parser


# === init =================================================================


def _cmd_init(args: argparse.Namespace) -> int:
    binary = os.environ.get("TAILSCALE_BIN", DEFAULT_BINARY)
    if not Path(binary).exists():
        print(
            f"error: Tailscale binary not found at {binary}\n"
            f"set TAILSCALE_BIN=/path/to/tailscale and retry",
            file=sys.stderr,
        )
        return EXIT_ERROR

    client = TailscaleClient(binary=binary)
    try:
        accounts = client.switch_list()
    except TailscaleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not accounts:
        print(
            "error: no Tailscale accounts signed in. Open Tailscale.app, sign "
            "into the accounts you want, then retry.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    config_path = paths.profiles_yaml()
    if config_path.exists() and not args.force:
        print(
            f"error: {config_path} already exists. Pass --force to overwrite.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    paths.ensure_home()
    # Build a starter profiles map: one entry per signed-in account.
    profiles_map: dict[str, dict[str, object]] = {}
    default_name: str | None = None
    for acct in accounts:
        # Synthesize a profile name. Prefer the tailnet (it's stable and
        # human-readable); fall back to the nickname; finally the id.
        name_seed = acct.tailnet or acct.nickname or f"acct-{acct.id}"
        name = _safe_profile_name(name_seed)
        # Collisions: append the id to disambiguate.
        if name in profiles_map:
            name = f"{name}-{acct.id}"
        profiles_map[name] = {
            "account_id": acct.id,
            "tailnet": acct.tailnet or None,
            "exit_node": None,
            "description": acct.account,
        }
        if acct.selected and default_name is None:
            default_name = name
    if default_name is None:
        # Pick the first profile if none was selected.
        default_name = next(iter(profiles_map))

    payload = {
        "default": default_name,
        "tailscale_binary": binary,
        "profiles": profiles_map,
    }
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    print(f"wrote {config_path}")
    print(f"default profile: {default_name}")
    print(f"  ({len(profiles_map)} profile(s) scaffolded — edit to add exit nodes)")
    return EXIT_OK


_PROFILE_NAME_SAFE_RE = __import__("re").compile(r"[^A-Za-z0-9._-]")


def _safe_profile_name(seed: str) -> str:
    cleaned = _PROFILE_NAME_SAFE_RE.sub("-", seed).strip("-.")
    return cleaned or "profile"


# === doctor ===============================================================


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Validate the userspace-per-identity model: config, the tailscaled daemon
    binary, the tailscale CLI, bws (for key self-resolve), and per-profile
    registration state."""
    import shutil

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"config: ok ({len(config.profiles.by_name)} profile(s))")

    ok = True

    # tailscaled (the headless userspace daemon) is required for the model.
    if Path(config.tailscaled_binary).exists():
        print(f"tailscaled: {config.tailscaled_binary}")
    else:
        print(
            f"tailscaled: NOT FOUND at {config.tailscaled_binary} "
            f"(install with `brew install tailscale`)",
            file=sys.stderr,
        )
        ok = False

    # tailscale CLI used to control per-identity daemons over their --socket.
    if Path(config.tailscale_cli_binary).exists():
        print(f"tailscale cli: {config.tailscale_cli_binary}")
    elif shutil.which("tailscale"):
        print(f"tailscale cli: {shutil.which('tailscale')} (PATH; config path missing)")
    else:
        print(
            f"tailscale cli: NOT FOUND at {config.tailscale_cli_binary}",
            file=sys.stderr,
        )
        ok = False

    # bws — lets first registration self-resolve auth keys without `bws run`.
    if any(p.auth_key_env for p in config.profiles.by_name.values()):
        if shutil.which("bws"):
            print("bws: present (auth keys self-resolve on first `up`)")
        else:
            print(
                "bws: NOT FOUND — first registration will need a `bws run` "
                "wrapper or interactive login",
                file=sys.stderr,
            )

    # Per-profile summary + whether a node has already been registered (statedir).
    for name in config.profiles.names():
        p = config.profiles.get(name)
        registered = (paths.instance_dir(name) / "state").exists()
        bits = [f"account={p.account_id}"]
        if p.exit_node:
            bits.append(f"exit={p.exit_node}")
        if p.auth_key_env:
            bits.append(f"key=${p.auth_key_env}")
        bits.append("registered" if registered else "not-yet-registered")
        print(f"  {name}: {', '.join(bits)}")

    return EXIT_OK if ok else EXIT_ERROR


# === profiles / reset =====================================================


def _cmd_profiles(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_ERROR
    for name in config.profiles.names():
        p = config.profiles.get(name)
        marker = " (default)" if name == config.profiles.default_name else ""
        exit_node = p.exit_node or "<none>"
        print(f"  {name}{marker}  account={p.account_id}  exit_node={exit_node}")
    return EXIT_OK


# === reset ================================================================


def _cmd_reset(args: argparse.Namespace) -> int:
    """Stop ALL running userspace instances and clear the registry.

    Each instance is a private userspace tailscaled, so this only affects
    tailctl-managed daemons — the GUI app and the browser's global identity
    are never touched.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_ERROR
    instances = RegistryStore().read().instances
    if instances:
        print("running instances:", file=sys.stderr)
        for name, inst in instances.items():
            print(f"  {name}: pid={inst.pid} refcount={inst.refcount}", file=sys.stderr)
    if not args.force:
        print(
            "error: tailctl reset stops all instances; pass --force to confirm.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    mgr = _build_instance_manager(config)
    stopped = mgr.teardown_all()
    print(f"stopped {stopped} instance(s)")
    return EXIT_OK


# === userspace-per-identity verbs ========================================


def _build_instance_manager(config: Config) -> InstanceManager:
    return InstanceManager(config)


def _resolve_owner(*, for_run: bool) -> tuple[int, float]:
    """Identify the process whose liveness owns this claim.

    - ``run`` claims are scoped to the run process itself (``getpid``) — if it
      crashes, reap releases the claim.
    - ``up``/``down`` claims should outlive the CLI invocation, so they bind to
      the parent shell / session (``$TAILCTL_OWNER_PID`` or ``getppid``); reap
      releases them when that session dies.
    """
    env_pid = os.environ.get("TAILCTL_OWNER_PID")
    if env_pid:
        pid = int(env_pid)
    elif for_run:
        pid = os.getpid()
    else:
        pid = os.getppid()
    try:
        return pid, psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        # Fall back to self if the named owner is already gone.
        me = psutil.Process(os.getpid())
        return me.pid, me.create_time()


def _print_unconfigured(profile: str, config: Config) -> None:
    """Agent-facing report: the requested tailnet/profile isn't configured."""
    print(
        f"error: profile {profile!r} is not configured in profiles.yaml.\n"
        f"  configured profiles: {', '.join(config.profiles.names()) or '(none)'}\n"
        f"  this tailnet is not available until it is bootstrapped — that needs a\n"
        f"  human to mint + store an auth key, then: tailctl bootstrap {profile} ...",
        file=sys.stderr,
    )


def _print_auth_instructions(result) -> None:
    print(
        f"profile {result.profile!r} needs a one-time login.", file=sys.stderr
    )
    if result.auth_url:
        print(f"  authenticate at: {result.auth_url}", file=sys.stderr)
        print(
            "  then re-run the same command — the instance reuses the saved login.",
            file=sys.stderr,
        )
    else:
        print(
            "  (could not read the auth URL yet; check "
            "`~/.tailctl/instances/<profile>/tailscaled.log`)",
            file=sys.stderr,
        )


def _cmd_up(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_ERROR
    mgr = _build_instance_manager(config)
    owner_pid, owner_ct = _resolve_owner(for_run=False)
    try:
        result = mgr.up(args.profile, owner_pid=owner_pid, owner_create_time=owner_ct)
    except UnknownProfileError:
        _print_unconfigured(args.profile, config)
        return EXIT_USAGE
    except InstanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if result.needs_auth:
        _print_auth_instructions(result)
        return EXIT_AUTH
    print(f"{result.profile}: up")
    print(f"  socks5: 127.0.0.1:{result.socks_port}  http: 127.0.0.1:{result.http_port}")
    for service, lp in (result.forwards or {}).items():
        print(f"  forward {service}: 127.0.0.1:{lp}")
    return EXIT_OK


def _cmd_down(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_ERROR
    mgr = _build_instance_manager(config)
    owner_pid, _ = _resolve_owner(for_run=False)
    mgr.down(args.profile, owner_pid=owner_pid)
    print(f"{args.profile}: down")
    return EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("error: no command given. Usage: tailctl run <profile> -- <cmd>", file=sys.stderr)
        return EXIT_USAGE
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_ERROR
    mgr = _build_instance_manager(config)
    owner_pid, owner_ct = _resolve_owner(for_run=True)
    try:
        result = mgr.up(args.profile, owner_pid=owner_pid, owner_create_time=owner_ct)
    except UnknownProfileError:
        _print_unconfigured(args.profile, config)
        return EXIT_USAGE
    except InstanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if result.needs_auth:
        _print_auth_instructions(result)
        return EXIT_AUTH

    env = dict(os.environ)
    env["ALL_PROXY"] = f"socks5h://127.0.0.1:{result.socks_port}"
    env["all_proxy"] = env["ALL_PROXY"]
    env["HTTP_PROXY"] = f"http://127.0.0.1:{result.http_port}"
    env["HTTPS_PROXY"] = env["HTTP_PROXY"]
    env["http_proxy"] = env["HTTP_PROXY"]
    env["https_proxy"] = env["HTTP_PROXY"]
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    env["no_proxy"] = env["NO_PROXY"]
    for service, lp in (result.forwards or {}).items():
        env[f"{service.upper()}_ADDR"] = f"127.0.0.1:{lp}"
    try:
        proc = __import__("subprocess").run(command, env=env, check=False)
        rc = proc.returncode
    except FileNotFoundError:
        print(f"error: command not found: {command[0]!r}", file=sys.stderr)
        rc = EXIT_ERROR
    finally:
        mgr.down(args.profile, owner_pid=owner_pid)
    return rc


def _cmd_reap(args: argparse.Namespace) -> int:
    """Drop dead-owner claims and stop orphaned/expired instances. Safe to run
    from cron/launchd as the janitor for crashed sessions."""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_ERROR
    reaped = _build_instance_manager(config).reap()
    print(f"reaped {len(reaped)} instance(s)" + (f": {', '.join(reaped)}" if reaped else ""))
    return EXIT_OK


def _cmd_ps(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_ERROR
    mgr = _build_instance_manager(config)
    mgr.reap()
    instances = mgr.list_instances()
    if args.json:
        from dataclasses import asdict

        print(json.dumps([asdict(i) for i in instances], indent=2, sort_keys=True))
        return EXIT_OK
    if not instances:
        print("no running instances")
        return EXIT_OK
    for inst in instances:
        print(
            f"  {inst.profile}  pid={inst.pid}  refcount={inst.refcount}  "
            f"socks=127.0.0.1:{inst.socks_port}  http=127.0.0.1:{inst.http_port}"
        )
        for f in inst.forwards:
            print(
                f"      forward {f.service}: 127.0.0.1:{f.local_port} "
                f"-> {f.remote_host}:{f.remote_port}"
            )
    return EXIT_OK


# === bootstrap ============================================================

_BOOT_ID_RE = __import__("re").compile(r"^[A-Za-z0-9._-]+$")


def _discover_account_id(config: Config, tailnet: str) -> str | None:
    """Look up an account_id by tailnet name from the signed-in GUI accounts."""
    try:
        accounts = TailscaleClient(binary=config.tailscale_binary).switch_list()
    except TailscaleError:
        return None
    for acct in accounts:
        if acct.tailnet == tailnet:
            return acct.id
    return None


def _render_profile_block(
    name: str,
    account_id: str,
    tailnet: str | None,
    exit_node: str | None,
    accept_routes: bool,
    auth_key_env: str,
    forwards: list[tuple[str, str, int]],
) -> str:
    """Render a profiles.yaml profile block as text (so existing comments in the
    file are preserved — a PyYAML round-trip would strip them)."""
    lines = [f"  {name}:", f"    account_id: {account_id}"]
    if tailnet:
        lines.append(f"    tailnet: {tailnet}")
    if exit_node:
        lines.append(f"    exit_node: {exit_node}")
    lines.append(f"    accept_routes: {'true' if accept_routes else 'false'}")
    lines.append(f"    auth_key_env: {auth_key_env}")
    if forwards:
        lines.append("    port_forwards:")
        for svc, host, port in forwards:
            lines.append(f"      - service: {svc}")
            lines.append(f"        remote_host: {host}")
            lines.append(f"        remote_port: {port}")
    return "\n".join(lines) + "\n"


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_ERROR

    name = args.name
    if not _BOOT_ID_RE.match(name):
        print(f"error: profile name {name!r} must match [A-Za-z0-9._-]", file=sys.stderr)
        return EXIT_USAGE
    if name in config.profiles.by_name and not args.force:
        print(
            f"error: profile {name!r} already exists. Pass --force to overwrite "
            f"(note: overwrite appends a duplicate key — edit profiles.yaml by hand "
            f"to replace).",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # Resolve account id: explicit, else discover by tailnet.
    account_id = args.account_id
    if not account_id and args.tailnet:
        account_id = _discover_account_id(config, args.tailnet)
        if not account_id:
            print(
                f"error: could not find a signed-in account for tailnet "
                f"{args.tailnet!r}. Sign into it in Tailscale.app, or pass "
                f"--account-id explicitly.",
                file=sys.stderr,
            )
            return EXIT_ERROR
    if not account_id:
        print("error: provide --account-id or --tailnet (to discover it)", file=sys.stderr)
        return EXIT_USAGE
    if not _BOOT_ID_RE.match(account_id):
        print(f"error: account-id {account_id!r} must match [A-Za-z0-9._-]", file=sys.stderr)
        return EXIT_USAGE

    # Parse port-forwards "svc=host:port".
    forwards: list[tuple[str, str, int]] = []
    for spec in args.port_forward:
        try:
            svc, hostport = spec.split("=", 1)
            host, port_s = hostport.rsplit(":", 1)
            port = int(port_s)
        except ValueError:
            print(f"error: bad --port-forward {spec!r}; expected SVC=HOST:PORT", file=sys.stderr)
            return EXIT_USAGE
        if not _BOOT_ID_RE.match(svc) or not host or not (1 <= port <= 65535):
            print(f"error: bad --port-forward {spec!r}", file=sys.stderr)
            return EXIT_USAGE
        forwards.append((svc, host, port))

    auth_key_env = args.auth_key_env or f"{name.replace('-', '_')}_ts_authkey"

    block = _render_profile_block(
        name, account_id, args.tailnet, args.exit_node,
        args.accept_routes, auth_key_env, forwards,
    )

    # Append under `profiles:` (last top-level key) — text append preserves the
    # file's existing comments. Then reload to validate the result parses.
    path = paths.profiles_yaml()
    original = path.read_text()
    path.write_text(original.rstrip("\n") + "\n\n" + block)
    try:
        load_config()
    except ConfigError as exc:
        path.write_text(original)  # roll back the append
        print(f"error: appended profile did not validate ({exc}); reverted.", file=sys.stderr)
        return EXIT_ERROR

    print(f"bootstrapped profile {name!r} (account={account_id}, tailnet={args.tailnet})")
    print("next steps to finish onboarding this tailnet:")
    print(f"  1. mint a REUSABLE auth key in {args.tailnet or 'the tailnet'}'s admin console")
    print(f"  2. store it in BWS under the key name: {auth_key_env}")
    print(f"  3. register the node:  tailctl up {name}")

    if args.register:
        print("\n--register: attempting first registration now...")
        ns = argparse.Namespace(profile=name)
        return _cmd_up(ns)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
