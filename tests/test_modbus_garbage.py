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


# ---- a battery that is slow, quiet or gone: what the client waits for, and why it says it failed ----------------------------------

import time as _time

from hp.drivers.modbus import describe_failure


def _reply(req: bytes, value: int = 7) -> bytes:
    return req[:4] + b"\x00\x05" + req[6:7] + b"\x03\x02" + value.to_bytes(2, "big")


async def _server(handle):
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


def test_a_slow_battery_is_waited_for_not_given_up_on_and_asked_again():
    async def go():
        conns = []

        async def handle(r, w):
            conns.append(1)
            while True:
                req = await r.read(64)
                if not req:
                    return
                await asyncio.sleep(0.5)                      # slower than the short wait (0.2 s), within the long one (1.2 s)
                w.write(_reply(req)); await w.drain()
        srv, port = await _server(handle)
        c = ModbusTcpClient("127.0.0.1", port, 1, timeout=0.2, patience=1.2)
        for _ in range(3):
            assert await c.read_holding(0, 1) == [7]
        assert len(conns) == 1, "the slow reply must be waited for on the same connection, not re-requested on a new one each time"
        assert c.reply_s and c.reply_s > 0.4
        await c.close(); srv.close()
    asyncio.run(go())


def test_a_connection_gone_quiet_is_replaced_quickly():
    async def go():
        conns = []

        async def handle(r, w):
            conns.append(1)
            n = 0
            while True:
                req = await r.read(64)
                if not req:
                    return
                n += 1
                if len(conns) == 1 and n > 1:                 # the first connection answers once, then silently stops (a dead socket)
                    continue
                w.write(_reply(req)); await w.drain()
        srv, port = await _server(handle)
        c = ModbusTcpClient("127.0.0.1", port, 1, timeout=0.2, patience=1.2)
        assert await c.read_holding(0, 1) == [7]
        t = _time.monotonic()
        assert await c.read_holding(0, 1) == [7]
        assert _time.monotonic() - t < 0.6 and len(conns) == 2      # the short wait, then a fresh connection: not the long wait on the dead one
        await c.close(); srv.close()
    asyncio.run(go())


def test_a_silent_battery_costs_one_long_wait_and_the_reason_says_so():
    async def go():
        async def handle(r, w):
            await asyncio.sleep(30)
        srv, port = await _server(handle)
        c = ModbusTcpClient("127.0.0.1", port, 1, timeout=0.2, patience=0.8)
        t = _time.monotonic()
        with pytest.raises(ModbusError) as ei:
            await c.read_holding(0, 1)
        took = _time.monotonic() - t
        assert 0.7 < took < 1.4, took                          # one long wait, not two
        assert ei.value.reason == "no_reply" and "did not answer in time" in describe_failure(ei.value)
        await c.close(); srv.close()
    asyncio.run(go())


def test_failure_reasons_refused_closed_and_garbage():
    async def go():
        srv, port = await _server(lambda r, w: None)
        srv.close(); await srv.wait_closed()                   # nothing listens any more
        c = ModbusTcpClient("127.0.0.1", port, 1, timeout=0.3)
        with pytest.raises(ModbusError) as ei:
            await c.read_holding(0, 1)
        assert ei.value.reason in ("refused", "no_connect")         # Windows lets the connect attempt time out instead of refusing it at once
        assert any(t in describe_failure(ei.value) for t in ("refused the connection", "no reply when connecting"))

        async def closes(r, w):
            await r.read(64); w.close()
        srv, port = await _server(closes)
        c = ModbusTcpClient("127.0.0.1", port, 1, timeout=0.3)
        with pytest.raises(ModbusError) as ei:
            await c.read_holding(0, 1)
        assert ei.value.reason == "closed" and "closed the connection" in describe_failure(ei.value)
        srv.close()

        srv, port = await _serve(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        c = ModbusTcpClient("127.0.0.1", port, 1, timeout=0.3)
        with pytest.raises(ModbusError) as ei:
            await c.read_holding(0, 1)
        assert ei.value.reason == "garbage" and "not a Modbus device" in describe_failure(ei.value)
        srv.close()
        assert describe_failure(ModbusError("device returned exception 2", code=2)) == "device returned exception 2"      # an answer with a Modbus exception keeps its own text
    asyncio.run(go())


def test_only_an_unreachable_host_counts_as_known_offline():
    from hp.drivers.modbus import ModbusError, known_down
    assert known_down(ModbusError("x", reason="unreachable"))
    for r in ("refused", "no_connect", "no_reply", "closed", "garbage", None):
        assert not known_down(ModbusError("x", reason=r))     # these can be another client holding the connection
