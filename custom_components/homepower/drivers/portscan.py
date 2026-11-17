"""Which of a handful of well-known ports a device on your own network is listening on.

Made for finding out whether a battery now offers a local connection (Modbus TCP, MQTT ...) after a firmware update. It only
opens a TCP connection to each port and listens for a few bytes; it never sends data, never tries a login and never scans a
range. It refuses any address that is not on a private network, so it cannot be used to probe the internet.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket

# A short, fixed list: the ports local control of an energy device is likely to use. Nothing else is ever tried.
PORTS: dict[int, str] = {
    22: "SSH", 23: "Telnet", 80: "HTTP", 443: "HTTPS", 502: "Modbus TCP", 1883: "MQTT", 4840: "OPC UA",
    5020: "Modbus TCP (alternate)", 7777: "(used by some devices)", 8000: "HTTP (alternate)", 8080: "HTTP (alternate)",
    8883: "MQTT over TLS", 9000: "(unassigned)",
}
CONNECT_TIMEOUT_S = 1.5
BANNER_WAIT_S = 0.4
PARALLEL = 8


# Exactly the ranges used inside a home or office network (Python's own is_private is wider: it also counts documentation ranges).
_LOCAL = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
                                            "::1/128", "fc00::/7", "fe80::/10")]


def _is_local(ip: ipaddress._BaseAddress) -> bool:
    return any(ip.version == n.version and ip in n for n in _LOCAL)


async def check_host(host: str) -> str:
    """The address to scan, or ValueError if it is not a device on a private network."""
    host = (host or "").strip()
    if not host or len(host) > 253 or any(c in host for c in " /\\?#@"):
        raise ValueError("enter the device's IP address, for example 192.168.1.50")
    try:
        ip = ipaddress.ip_address(host)
        found = [ip]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as err:
            raise ValueError(f"could not find '{host}' on your network") from err
        found = sorted({ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}, key=str)
        if not found:
            raise ValueError(f"could not find '{host}' on your network")
    if not all(_is_local(ip) for ip in found):
        raise ValueError("this only scans devices on your own network (a private address such as 192.168.x.x or 10.x.x.x)")
    return str(found[0])


def _printable(raw: bytes) -> str:
    text = "".join(chr(b) if 32 <= b < 127 else " " for b in raw[:80]).strip()
    return " ".join(text.split())[:60]


async def _probe(ip: str, port: int, timeout: float) -> dict:
    row = {"port": port, "service": "", "state": "closed", "banner": ""}
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except ConnectionRefusedError:
        return row
    except (asyncio.TimeoutError, TimeoutError):
        row["state"] = "no answer"                   # a firewall, or nothing there at all
        return row
    except OSError:
        return row
    row["state"] = "open"
    try:
        row["banner"] = _printable(await asyncio.wait_for(reader.read(64), BANNER_WAIT_S))   # listen only: nothing is sent
    except (asyncio.TimeoutError, TimeoutError, OSError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    return row


async def scan(host: str, ports: dict[int, str] | None = None, timeout: float = CONNECT_TIMEOUT_S) -> dict:
    """Scan one device. Returns {"host", "address", "results": [{port, service, state, banner}]}, ports in order."""
    ports = PORTS if ports is None else ports
    ip = await check_host(host)
    gate = asyncio.Semaphore(PARALLEL)

    async def one(port: int) -> dict:
        async with gate:
            row = await _probe(ip, port, timeout)
        row["service"] = ports.get(port, "")
        return row

    rows = await asyncio.gather(*(one(p) for p in sorted(ports)))
    return {"host": host.strip(), "address": ip, "results": list(rows)}
