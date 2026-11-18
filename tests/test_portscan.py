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


# ------------------------------------------------------------------ the wide scan
def test_wide_scan_lists_only_open_ports_and_counts_the_rest():
    import hp.drivers.portscan as ps

    async def go():
        async def hello(reader, writer):
            writer.write(b"EF local\r\n")
            await writer.drain()
            writer.close()
        s1 = await asyncio.start_server(hello, "127.0.0.1", 0)
        s2 = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        p1, p2 = s1.sockets[0].getsockname()[1], s2.sockets[0].getsockname()[1]
        s3 = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        p3 = s3.sockets[0].getsockname()[1]
        s3.close(); await s3.wait_closed()
        out = await ps.scan_wide("127.0.0.1", ports=[p1, p2, p3], timeout=1.0)
        s1.close(); s2.close()
        return out, (p1, p2, p3)

    out, (p1, p2, p3) = run(go())
    assert out["wide"] is True and out["total"] == 3 and out["complete"] is True and out["address"] == "127.0.0.1"
    assert [r["port"] for r in out["results"]] == sorted([p1, p2]) and all(r["state"] == "open" for r in out["results"])
    assert {r["port"]: r["banner"] for r in out["results"]}[p1] == "EF local"
    c = out["counts"]
    assert c["open"] == 2 and c["closed"] + c["no answer"] == 1 and c["skipped"] == 0
    assert ps._wide_running is False                           # released after the scan


def test_wide_scan_gives_up_politely_when_the_device_is_too_slow():
    import hp.drivers.portscan as ps
    out = run(ps.scan_wide("127.0.0.1", ports=range(40000, 40050), deadline_s=0))
    assert out["complete"] is False and out["counts"]["skipped"] == 50 and out["results"] == []
    assert ps._wide_running is False


def test_only_one_wide_scan_at_a_time_and_the_flag_is_always_released(monkeypatch):
    import hp.drivers.portscan as ps
    monkeypatch.setattr(ps, "_wide_running", True)
    with pytest.raises(ValueError, match="already running"):
        run(ps.scan_wide("127.0.0.1", ports=[1]))
    monkeypatch.setattr(ps, "_wide_running", False)

    async def boom(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(ps, "_probe", boom)
    with pytest.raises(RuntimeError):
        run(ps.scan_wide("127.0.0.1", ports=[1, 2]))
    assert ps._wide_running is False                           # a crash must not lock wide scans for good


def test_wide_scan_refuses_public_addresses_and_defaults_to_ports_1_to_10000():
    import hp.drivers.portscan as ps
    with pytest.raises(ValueError, match="own network"):
        run(ps.scan_wide("8.8.8.8", ports=[80]))
    assert ps._wide_running is False
    assert ps.WIDE_PORTS[0] == 1 and ps.WIDE_PORTS[-1] == 10000 and len(ps.WIDE_PORTS) == 10000
