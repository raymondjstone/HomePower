"""Find batteries without the user having to know anything: scan the LAN for Anker Modbus devices."""
from __future__ import annotations

import asyncio
import ipaddress

from .modbus import ModbusError, ModbusTcpClient, detect_word_order, to_i32, to_text


def subnet_hosts(address: str, prefix: int, limit: int = 254) -> list[str]:
    """Hosts of the adapter's subnet (never wider than a /24 around the address)."""
    ip = ipaddress.ip_address(address)
    net = ipaddress.ip_network(f"{address}/{max(prefix, 24)}", strict=False)
    return [str(h) for h in net.hosts() if h != ip][:limit]


async def probe_host(host: str, port: int = 502, unit: int = 1, timeout: float = 1.0) -> dict | None:
    """Return details if an Anker-style battery answers on host:port, else None."""
    client = ModbusTcpClient(host, port, unit, timeout=timeout)
    try:
        regs = await client.read_input(10000, 51)
        soc = regs[14]
        if not 0 <= soc <= 100:
            return None
        order = detect_word_order([(regs[i], regs[i + 1]) for i in (2, 8, 10, 12, 36, 38)]) or "big"
        info = {"host": host, "port": port, "soc": soc, "power_w": -to_i32(regs[8], regs[9], order)}
        for key, addr, count in (("model", 32768, 16), ("serial", 10100, 8)):
            try:
                info[key] = to_text(await client.read_input(addr, count)) or None
            except ModbusError:
                info[key] = None
        return info
    except (ModbusError, IndexError, OSError):
        return None
    finally:
        await client.close()


async def scan(hosts: list[str], port: int = 502, unit: int = 1, concurrency: int = 64) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)

    async def one(h: str):
        async with sem:
            try:  # cheap TCP check first so dead addresses cost ~0.4 s, not a Modbus timeout
                _, w = await asyncio.wait_for(asyncio.open_connection(h, port), 0.4)
                w.close()
            except (OSError, asyncio.TimeoutError):
                return None
            return await probe_host(h, port, unit)

    return [r for r in await asyncio.gather(*(one(h) for h in hosts)) if r]
