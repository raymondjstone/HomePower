"""Minimal asyncio Modbus TCP client (function codes 03, 04, 06, 16). No third-party packages."""
from __future__ import annotations

import asyncio
import struct


class ModbusError(Exception):
    def __init__(self, msg: str, code: int | None = None) -> None:
        super().__init__(msg)
        self.code = code  # Modbus exception code, if the device answered with one


def _signed(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


def to_u32(a: int, b: int, order: str = "big") -> int:
    """32-bit unsigned from two registers. order="big": first register is the HIGH word."""
    return (a << 16) | b if order == "big" else (b << 16) | a


def to_i32(a: int, b: int, order: str = "big") -> int:
    return _signed(to_u32(a, b, order))


def from_i32(value: int, order: str = "big") -> list[int]:
    v = value & 0xFFFFFFFF
    hi, lo = v >> 16, v & 0xFFFF
    return [hi, lo] if order == "big" else [lo, hi]


def detect_word_order(pairs: list[tuple[int, int]], limit: int = 200_000) -> str | None:
    """Work out which register of a 32-bit pair is the high word.

    A power value in the wrong order looks like millions of watts, so for every non-trivial pair
    the order that gives a plausible magnitude wins. Returns None when the data can't tell
    (e.g. everything is zero).
    """
    big = little = 0
    for a, b in pairs:
        vb, vl = to_i32(a, b, "big"), to_i32(a, b, "little")
        if vb == vl:
            continue
        ok_b, ok_l = abs(vb) <= limit, abs(vl) <= limit
        if ok_b and not ok_l:
            big += 1
        elif ok_l and not ok_b:
            little += 1
    return "big" if big > little else "little" if little > big else None


def to_text(regs: list[int]) -> str:
    raw = b"".join(struct.pack(">H", r) for r in regs)
    return "".join(c for c in raw.decode("latin-1") if c.isprintable()).strip()


class ModbusTcpClient:
    def __init__(self, host: str, port: int = 502, unit: int = 1, timeout: float = 3.0) -> None:
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None
        self._tid = 0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._w is not None:
            self._w.close()
            try:
                await self._w.wait_closed()
            except OSError:
                pass
        self._r = self._w = None

    async def _txn(self, pdu: bytes) -> bytes:
        async with self._lock:
            last: Exception | None = None
            for _ in range(2):  # one transparent reconnect
                try:
                    if self._w is None:
                        self._r, self._w = await asyncio.wait_for(
                            asyncio.open_connection(self.host, self.port), self.timeout)
                    self._tid = (self._tid + 1) & 0xFFFF
                    self._w.write(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit) + pdu)
                    await self._w.drain()
                    tid, _proto, length, _unit = struct.unpack(
                        ">HHHB", await asyncio.wait_for(self._r.readexactly(7), self.timeout))
                    body = await asyncio.wait_for(self._r.readexactly(length - 1), self.timeout)
                    if tid != self._tid:
                        raise OSError("transaction id mismatch")
                    break
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as err:
                    last = err
                    await self.close()
            else:
                raise ModbusError(f"{self.host}:{self.port} unreachable: {last!r}")
        if body[0] & 0x80:
            raise ModbusError(f"device returned exception {body[1]}", code=body[1])
        return body

    async def _read(self, fc: int, addr: int, count: int) -> list[int]:
        out: list[int] = []
        while count > 0:
            n = min(count, 125)
            body = await self._txn(struct.pack(">BHH", fc, addr, n))
            if len(body) < 2 + 2 * n:
                raise ModbusError("short response")
            out += list(struct.unpack(f">{n}H", body[2:2 + 2 * n]))
            addr, count = addr + n, count - n
        return out

    async def read_holding(self, addr: int, count: int) -> list[int]:
        return await self._read(3, addr, count)

    async def read_input(self, addr: int, count: int) -> list[int]:
        return await self._read(4, addr, count)

    async def write_single(self, addr: int, value: int) -> None:
        await self._txn(struct.pack(">BHH", 6, addr, value & 0xFFFF))

    async def write_multiple(self, addr: int, values: list[int]) -> None:
        data = struct.pack(f">{len(values)}H", *[v & 0xFFFF for v in values])
        await self._txn(struct.pack(">BHHB", 16, addr, len(values), len(data)) + data)

    async def write_register(self, addr: int, value: int) -> None:
        """Write one register; some devices only accept function 16."""
        try:
            await self.write_single(addr, value)
        except ModbusError as err:
            if err.code != 1:  # 1 = illegal function
                raise
            await self.write_multiple(addr, [value])
