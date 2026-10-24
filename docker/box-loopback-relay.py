#!/usr/bin/env python3
"""Inside the isolated box: `box-loopback-relay.py LISTEN_PORT TARGET_HOST:PORT`.

The model the operator set up "on this machine" is reached as
http://127.0.0.1:PORT or http://host.docker.internal:PORT — the address the
setup page tells everyone to use. In the isolated box neither is this machine:
the box has its own loopback and no route out. This forwards the box's
127.0.0.1:PORT to the model relay (`model:PORT`), so those URLs keep working
everywhere they are used; the relay itself reaches only the one configured
upstream. Started by docker/entrypoint.sh. Stdlib only.
"""
import asyncio
import sys


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def main(listen_port: int, target: str) -> None:
    host, _, port = target.rpartition(":")

    async def handle(reader, writer):
        try:
            up_r, up_w = await asyncio.wait_for(
                asyncio.open_connection(host, int(port)), timeout=15)
        except (OSError, asyncio.TimeoutError):
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        for w in (up_w, writer):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: box-loopback-relay.py LISTEN_PORT HOST:PORT", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(main(int(sys.argv[1]), sys.argv[2]))
    except OSError as exc:            # the port is taken: a local model server
        print(f"box-loopback-relay: {exc}", file=sys.stderr)
        sys.exit(1)
