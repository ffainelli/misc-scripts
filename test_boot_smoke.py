"""Console transport tests using a local TCP server, without lab hardware."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from boot_smoke import BootFailure, Console


class ConsoleTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_and_tcp_preserve_bytes(self):
        for transport in ("raw", "tcp"):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                received = asyncio.get_running_loop().create_future()
                finished = asyncio.get_running_loop().create_future()
                release = asyncio.Event()
                payload = b"boot\x00\xff\xfb\x01\xff\xff\r\n> "

                async def handle(reader, writer):
                    try:
                        writer.write(payload)
                        await writer.drain()
                        received.set_result(await reader.readuntil(b"\r"))
                        await release.wait()
                    finally:
                        writer.close()
                        await writer.wait_closed()
                        finished.set_result(None)

                async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    log = Path(directory) / "console.log"
                    console = Console({"transport": transport, "host": "127.0.0.1", "port": port}, log)
                    try:
                        await console.connect()
                        await console.expect(r"> ", 2)
                        await console.send("boot kernel", newline=True)
                        self.assertEqual(await asyncio.wait_for(received, 2), b"boot kernel\r")
                    finally:
                        release.set()
                        await console.close()
                        await asyncio.wait_for(finished, 2)
                    self.assertEqual(log.read_bytes(), payload)

    async def test_unknown_transport(self):
        console = Console({"transport": "rawlp", "host": "localhost", "port": 1}, Path("unused.log"))
        with self.assertRaisesRegex(BootFailure, "Unknown console transport: 'rawlp'"):
            await console._open_connection()


if __name__ == "__main__":
    unittest.main()
