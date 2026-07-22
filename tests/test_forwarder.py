"""Forwarder relay test: a client hitting the local port reaches the upstream
through a (fake) SOCKS5 proxy, with bytes piped both ways."""

from __future__ import annotations

import asyncio
import socket as _socket

from tailctl import forwarder


def _free_port() -> int:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _fake_socks5_echo_server(port: int) -> asyncio.AbstractServer:
    """Minimal SOCKS5 (no-auth) server that, after CONNECT, echoes bytes —
    standing in for both the proxy and the upstream service."""

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        ver_n = await r.readexactly(2)  # ver, nmethods
        await r.readexactly(ver_n[1])  # methods
        w.write(b"\x05\x00")  # no-auth
        await w.drain()
        head = await r.readexactly(4)  # ver,cmd,rsv,atyp
        atyp = head[3]
        if atyp == 0x03:
            ln = (await r.readexactly(1))[0]
            await r.readexactly(ln)
        elif atyp == 0x01:
            await r.readexactly(4)
        await r.readexactly(2)  # port
        # success reply with BND.ADDR 0.0.0.0:0
        w.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await w.drain()
        # echo loop
        while data := await r.read(4096):
            w.write(data)
            await w.drain()
        w.close()

    return await asyncio.start_server(handle, "127.0.0.1", port)


async def _scenario() -> bytes:
    socks_port = _free_port()
    local_port = _free_port()
    socks_server = await _fake_socks5_echo_server(socks_port)

    serve_task = asyncio.create_task(
        forwarder.serve(local_port, socks_port, "service.tailnet", 9000)
    )
    await asyncio.sleep(0.2)  # let both servers bind

    r, w = await asyncio.open_connection("127.0.0.1", local_port)
    w.write(b"ping-through-tailnet")
    await w.drain()
    echoed = await asyncio.wait_for(r.readexactly(len(b"ping-through-tailnet")), timeout=5)
    w.close()

    serve_task.cancel()
    socks_server.close()
    return echoed


def test_relay_pipes_through_socks5() -> None:
    echoed = asyncio.run(_scenario())
    assert echoed == b"ping-through-tailnet"
