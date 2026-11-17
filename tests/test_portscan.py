"""The local port scan: it finds open ports, never sends anything, and refuses anything off the private network."""
import asyncio

import pytest

from hp.drivers.portscan import PORTS, check_host, scan


def run(coro):
    return asyncio.run(coro)


def test_only_private_addresses_are_accepted():
    for ok in ("192.168.1.50", "10.0.0.7", "172.16.4.4", "127.0.0.1", "169.254.1.1", " 192.168.1.50 ", "localhost"):
        assert run(check_host(ok)) in ("192.168.1.50", "10.0.0.7", "172.16.4.4", "127.0.0.1", "169.254.1.1", "::1")
    for bad in ("8.8.8.8", "1.1.1.1", "203.0.113.9", "0.0.0.0", "224.0.0.1", "", "   ", "192.168.1.1/24", "192.168.1.1 8.8.8.8",
                "http://192.168.1.1", "a" * 300, "user@192.168.1.1", "100.64.0.1", "::ffff:8.8.8.8", "2001:4860:4860::8888"):
        with pytest.raises(ValueError):
            run(check_host(bad))


def test_the_port_list_is_short_and_fixed():
    assert len(PORTS) <= 15 and 502 in PORTS and 1883 in PORTS and all(1 <= p <= 65535 for p in PORTS)


def test_open_closed_and_a_banner_are_told_apart_and_nothing_is_sent():
    received = []

    async def go():
        async def hello(reader, writer):
            writer.write(b"HELLO device v1\x00\x01")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(10), 0.3) if False else b""
            received.append(data)
            writer.close()

        async def silent(reader, writer):
            try:
                received.append(await asyncio.wait_for(reader.read(10), 0.8))     # whatever the scanner sends would land here
            except asyncio.TimeoutError:
                received.append(b"")
            writer.close()

        s1 = await asyncio.start_server(hello, "127.0.0.1", 0)
        s2 = await asyncio.start_server(silent, "127.0.0.1", 0)
        p1, p2 = s1.sockets[0].getsockname()[1], s2.sockets[0].getsockname()[1]
        s3 = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        p3 = s3.sockets[0].getsockname()[1]
        s3.close(); await s3.wait_closed()                                        # a port that is now closed
        out = await scan("127.0.0.1", {p1: "talks", p2: "quiet", p3: "gone"}, timeout=1.0)
        s1.close(); s2.close()
        return out, (p1, p2, p3)

    out, (p1, p2, p3) = run(go())
    rows = {r["port"]: r for r in out["results"]}
    assert rows[p1]["state"] == "open" and rows[p1]["banner"] == "HELLO device v1" and rows[p1]["service"] == "talks"
    assert rows[p2]["state"] == "open" and rows[p2]["banner"] == ""
    assert rows[p3]["state"] in ("closed", "no answer")           # Windows reports a closed local port as silent, Linux as refused
    assert all(d == b"" for d in received), received                              # the scanner sent nothing at all
    assert [r["port"] for r in out["results"]] == sorted(rows) and out["address"] == "127.0.0.1"


def test_a_silent_address_is_reported_as_no_answer(monkeypatch):
    async def never(*a, **k):
        await asyncio.sleep(10)
    monkeypatch.setattr(asyncio, "open_connection", never)
    out = run(scan("192.168.1.50", {502: "Modbus TCP"}, timeout=0.05))
    assert out["results"][0]["state"] == "no answer"


def test_a_banner_is_made_printable_and_short():
    from hp.drivers.portscan import _printable
    assert _printable(b"\x00\x01OK\xff\xfe  fine\r\n") == "OK fine"
    assert len(_printable(b"x" * 500)) <= 60
