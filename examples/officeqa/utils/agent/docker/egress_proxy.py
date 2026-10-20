"""The Codex containers' only way out: an HTTPS (CONNECT) proxy that opens tunnels to the allowed hosts and nothing
else.

The containers sit on an internal Docker network (no route out); this proxy is on that network and on the default one.
Codex reaches its provider through it (HTTPS_PROXY); every other connection is refused. Each decision is printed, one
line per connection: `<time> allow|deny <method> <host>:<port> from <address>` (`fail` when an allowed host could not
be reached), so `docker logs` shows everything the containers tried to reach.

    python3 egress_proxy.py [port] [host:port ...]     (default 3128 api.fireworks.ai:443)
"""
from __future__ import annotations

import asyncio
import sys
import time


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """One direction of a tunnel; at its end the other side is told so, and can still answer."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except OSError:
        writer.close()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, allowed: set[str]) -> None:
    peer = writer.get_extra_info("peername")
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
        writer.close()
        return
    method, target, *_ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ") + ["", ""]
    ok = method == "CONNECT" and target.lower() in allowed
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {'allow' if ok else 'deny'} {method} {target} from {peer[0]}",
          flush=True)
    if not ok:
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    host, port = target.rsplit(":", 1)
    try:
        up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 30)
    except (OSError, asyncio.TimeoutError) as e:
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} fail {target}: {e}", flush=True)
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
    writer.close()
    up_writer.close()


async def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3128
    allowed = {a.lower() for a in sys.argv[2:]} or {"api.fireworks.ai:443"}
    server = await asyncio.start_server(lambda r, w: handle(r, w, allowed), "0.0.0.0", port)
    print(f"egress proxy on :{port}, allowing {sorted(allowed)}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
