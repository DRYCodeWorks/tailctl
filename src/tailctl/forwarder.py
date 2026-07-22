"""Localhost TCP forward through a userspace tailscaled SOCKS5 proxy.

This is the native-DB path: clients that ignore proxy env vars (ClickHouse
native :9000, Postgres, SQL Server) connect to a stable ``127.0.0.1:<local_port>``
and the relay tunnels each connection through the profile's userspace tailscaled
SOCKS5 proxy to ``<remote_host>:<remote_port>`` on the tailnet.

Validated end-to-end in the Phase 0 spike (a non-proxy curl reached a tailnet
Caddy through this exact relay). Run as a detached process per forward:

    python -m tailctl.forwarder <local_port> <socks_port> <remote_host> <remote_port>

The instance manager spawns one per configured port-forward and records its pid
in the registry so it can be reaped with the instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import sys


async def socks5_connect(
    proxy_host: str, proxy_port: int, dst_host: str, dst_port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a SOCKS5 (no-auth) CONNECT tunnel to dst through the proxy."""
    r, w = await asyncio.open_connection(proxy_host, proxy_port)
    w.write(b"\x05\x01\x00")  # version 5, 1 method, no-auth
    await w.drain()
    if await r.readexactly(2) != b"\x05\x00":
        raise RuntimeError("SOCKS5 no-auth not accepted")
    host = dst_host.encode()
    # CONNECT, ATYP=domain — let the proxy resolve the name on the tailnet side.
    w.write(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", dst_port))
    await w.drain()
    rep = await r.readexactly(4)
    if rep[1] != 0x00:
        raise RuntimeError(f"SOCKS5 connect failed, REP={rep[1]}")
    atyp = rep[3]
    if atyp == 0x01:
        await r.readexactly(4)
    elif atyp == 0x04:
        await r.readexactly(16)
    elif atyp == 0x03:
        ln = (await r.readexactly(1))[0]
        await r.readexactly(ln)
    await r.readexactly(2)  # bound port
    return r, w


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    """Copy src→dst until src EOF, then HALF-close dst (write_eof) so the other
    direction can still deliver its response. Does NOT fully close dst — that
    would truncate request/response protocols (ClickHouse native, Postgres, TDS)
    where a client finishes sending but still awaits the server's reply."""
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    except Exception:  # noqa: BLE001
        # On a real transport error, fully close so the peer doesn't hang.
        with contextlib.suppress(Exception):
            dst.close()
        return
    # Clean EOF on src → propagate a half-close (FIN) to dst, keep reading the
    # reverse direction.
    try:
        if dst.can_write_eof():
            dst.write_eof()
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            dst.close()


async def serve(
    local_port: int, socks_port: int, remote_host: str, remote_port: int
) -> None:
    async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            pr, pw = await socks5_connect("127.0.0.1", socks_port, remote_host, remote_port)
        except Exception as e:  # noqa: BLE001
            print(f"forwarder: upstream connect failed: {e}", file=sys.stderr)
            with contextlib.suppress(Exception):
                cw.close()
            return
        # Both directions run to completion (each half-closes its peer on EOF);
        # only once both finish do we fully close both writers.
        await asyncio.gather(_pipe(cr, pw), _pipe(pr, cw))
        for w in (cw, pw):
            with contextlib.suppress(Exception):
                w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", local_port)
    print(
        f"forwarder: 127.0.0.1:{local_port} -> socks5 127.0.0.1:{socks_port} "
        f"-> {remote_host}:{remote_port}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 4:
        print(
            "usage: python -m tailctl.forwarder <local_port> <socks_port> "
            "<remote_host> <remote_port>",
            file=sys.stderr,
        )
        return 2
    lp, sp, rh, rp = int(args[0]), int(args[1]), args[2], int(args[3])
    try:
        asyncio.run(serve(lp, sp, rh, rp))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
