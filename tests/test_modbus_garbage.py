"""A port that is not Modbus (or a broken device) must give a clean ModbusError, never a raw ValueError / IndexError."""
import asyncio

import pytest

from hp.drivers.modbus import ModbusError, ModbusTcpClient


async def _serve(payload: bytes):
    async def handle(r, w):
        try:
            await r.read(64)
            w.write(payload)
            await w.drain()
            await asyncio.sleep(0.2)
        finally:
            w.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


@pytest.mark.parametrize("payload", [
    b"HTTP/1.1 400 Bad Request\r\n\r\n",                  # a web server on that port
    b"\x00\x01\x00\x00\x00\x00\x01",                      # length 0
    b"\x00\x01\x00\x00\x00\x01\x01",                      # length 1: no function code
    b"\x00\x01\x00\x00\xff\xff\x01",                      # absurd length
    b"\x00\x01\x00\x07\x00\x03\x01\x03\x02",              # wrong protocol id
    b"",                                                  # closed without a reply
])
def test_garbage_reply_is_a_clean_modbus_error(payload):
    async def go():
        srv, port = await _serve(payload)
        try:
            c = ModbusTcpClient("127.0.0.1", port, 1, timeout=0.5)
            with pytest.raises(ModbusError):
                await c.read_holding(0, 1)
            await c.close()
        finally:
            srv.close()
    asyncio.run(go())


def test_short_exception_reply_is_still_a_modbus_error():
    async def go():
        async def handle(r, w):
            req = await r.read(64)
            w.write(req[:4] + b"\x00\x02" + req[6:7] + b"\x83")      # exception reply with no code byte
            await w.drain()
            await asyncio.sleep(0.2)
            w.close()
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        c = ModbusTcpClient("127.0.0.1", port, 1, timeout=0.5)
        with pytest.raises(ModbusError):
            await c.read_holding(0, 1)
        await c.close()
        srv.close()
    asyncio.run(go())
