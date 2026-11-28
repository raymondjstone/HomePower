"""Find batteries without the user having to know anything: scan the LAN for Anker Modbus devices."""
from __future__ import annotations
from typing import Any

import asyncio
import ipaddress

from .modbus import ModbusError, ModbusTcpClient, detect_word_order, to_i32, to_text, to_text_low_first


_PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")]


def subnet_hosts(address: str, prefix: int, limit: int = 254) -> list[str]:
    """Hosts of the adapter's subnet (never wider than a /24 around the address)."""
    ip = ipaddress.ip_address(address)
    if not (ip.version == 4 and any(ip in n for n in _PRIVATE)):
        return []                                  # never scan a public network (a host with a public address on an adapter)
    net = ipaddress.ip_network(f"{address}/{max(prefix, 24)}", strict=False)
    return [str(h) for h in net.hosts() if h != ip][:limit]


async def read_serial(client: ModbusTcpClient, x1: bool) -> str | None:
    """An Anker battery's serial number (10100), read the same way by the network scan and by the battery's own driver, so the
    two can be compared to recognise a battery that has moved to another address. The X1 keeps it low byte first."""
    try:
        return (to_text_low_first if x1 else to_text)(await client.read_input(10100, 12 if x1 else 8)) or None
    except ModbusError:
        return None


async def probe_host(host: str, port: int = 502, unit: int = 1, timeout: float = 1.0) -> dict[str, Any] | None:
    """Return details if an Anker-style battery answers on host:port, else None."""
    client = ModbusTcpClient(host, port, unit, timeout=timeout)
    try:
        regs = await client.read_input(10000, 51)
        soc = regs[14]
        if not 0 <= soc <= 100:
            return None
        order = detect_word_order([(regs[i], regs[i + 1]) for i in (2, 8, 10, 12, 36, 38)]) or "big"
        info = {"host": host, "port": port, "soc": soc, "power_w": -to_i32(regs[8], regs[9], order)}
        # An X1 keeps its model at 10090 and stores text low byte first (as its driver reads it); a Solarbank has its model at
        # 32768, high byte first. Reading an X1 the Solarbank way gave no model and a serial with every character pair swapped.
        try:
            x1_model = to_text_low_first(await client.read_input(10090, 10)) or None
        except ModbusError:
            x1_model = None
        x1 = bool(x1_model and x1_model.upper().startswith("X1"))
        if x1:
            info["model"] = x1_model
        else:
            try:
                info["model"] = to_text(await client.read_input(32768, 16)) or None
            except ModbusError:
                info["model"] = None
        info["serial"] = await read_serial(client, x1)
        info["x1"] = x1
        return info
    except Exception:  # noqa: BLE001 - any device may answer on 502 (an inverter, a PLC): whatever it sends, it is just "not an Anker"
        return None                     # and must never stop the scan of the other addresses
    finally:
        await client.close()


async def scan(hosts: list[str], port: int = 502, unit: int = 1, concurrency: int = 64) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)

    async def one(h: str) -> dict[str, Any] | None:
        async with sem:
            try:  # cheap TCP check first so dead addresses cost ~0.4 s, not a Modbus timeout
                _, w = await asyncio.wait_for(asyncio.open_connection(h, port), 0.4)
            except (OSError, asyncio.TimeoutError):
                return None
            w.close()
            try:
                await w.wait_closed()           # let it finish closing (no "transport not closed" warning)
            except OSError:
                pass
            try:
                return await probe_host(h, port, unit)
            except Exception:  # noqa: BLE001 - one address can never stop the scan of the others
                return None

    return [r for r in await asyncio.gather(*(one(h) for h in hosts)) if r]
