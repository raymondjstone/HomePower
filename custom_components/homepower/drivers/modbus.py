"""Minimal asyncio Modbus TCP client (function codes 03, 04, 06, 16). No third-party packages."""
from __future__ import annotations

import asyncio
import struct
import time


class ModbusError(Exception):
    def __init__(self, msg: str, code: int | None = None, reason: str | None = None) -> None:
        super().__init__(msg)
        self.code = code  # Modbus exception code, if the device answered with one
        self.reason = reason  # why there was no answer: refused / no_connect / no_reply / closed / garbage


class _BadReply(OSError):
    """Something answered that is not a Modbus TCP reply."""


_WHY = {
    "refused": "the battery refused the connection (its Modbus service is not running)",
    "no_connect": "no reply when connecting (the battery is not reachable on the network)",
    "no_reply": "connected, but the battery did not answer in time",
    "closed": "the battery closed the connection",
    "garbage": "something answered that is not a Modbus device",
    "unreachable": "the network reports the battery is not there (host unreachable)",
}

# errno values for "no route to the host / network" on Linux, macOS and Windows: the network itself says nothing is there.
_UNREACHABLE = {113, 101, 65, 51, 10065, 10051}


def known_down(err: Exception) -> bool:
    """True when a failure shows the battery is off the network, not merely busy (another Modbus client holding it, a slow
    reply): only then is it shown offline straight away; other failures are shown as stale readings for a while first."""
    return getattr(err, "reason", None) == "unreachable"


def describe_failure(err: Exception) -> str:
    """A plain-words reason for a failed read, for the panel and the log."""
    return _WHY.get(getattr(err, "reason", None) or "", "") or str(err)


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


def to_text_low_first(regs: list[int]) -> str:
    """Text where each register holds its two characters low byte first (the Anker X1's string registers)."""
    raw = b"".join(struct.pack("<H", r) for r in regs)
    return "".join(c for c in raw.decode("latin-1") if c.isprintable()).strip()


class ModbusTcpClient:
    def __init__(self, host: str, port: int = 502, unit: int = 1, timeout: float = 3.0, patience: float | None = None) -> None:
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        # How long a FRESH connection (or a battery that has been slow to answer) is waited for. A request is never re-sent while a
        # slow reply might still be on its way: a battery busy with its own cloud connection can take several seconds to answer.
        self.patience = patience if patience is not None else 2 * timeout
        self._slow = False                  # the last reply took longer than the short wait
        self.reply_s: float | None = None   # how long the last reply took
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
            reason = "closed"
            body = None
            for _ in range(2):  # one transparent reconnect
                patient, phase = False, "connect"
                try:
                    fresh = self._w is None
                    if fresh:
                        self._r, self._w = await asyncio.wait_for(
                            asyncio.open_connection(self.host, self.port), self.timeout)
                    # An old connection that has gone quiet may simply be dead, so it gets the short wait and then a fresh one. A fresh
                    # connection, or a battery that has been slow to answer, gets the long wait: its request is not sent again meanwhile.
                    wait = self.patience if (fresh or self._slow) else self.timeout
                    patient, phase = wait > self.timeout, "reply"
                    self._tid = (self._tid + 1) & 0xFFFF
                    self._w.write(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit) + pdu)
                    await self._w.drain()
                    sent = time.monotonic()
                    tid, _proto, length, _unit = struct.unpack(
                        ">HHHB", await asyncio.wait_for(self._r.readexactly(7), wait))
                    if not 2 <= length <= 260 or _proto != 0:           # not a Modbus reply (another service on this port, or garbage)
                        raise _BadReply("not a Modbus TCP reply")
                    body = await asyncio.wait_for(self._r.readexactly(length - 1), wait)
                    if tid != self._tid:
                        raise _BadReply("transaction id mismatch")
                    self.reply_s = time.monotonic() - sent
                    self._slow = self.reply_s > self.timeout
                    break
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as err:
                    last, body = err, None
                    if isinstance(err, _BadReply):
                        reason = "garbage"
                    elif isinstance(err, ConnectionRefusedError):
                        reason = "refused"
                    elif getattr(err, "errno", None) in _UNREACHABLE or getattr(err, "winerror", None) in _UNREACHABLE:
                        reason = "unreachable"
                    elif isinstance(err, asyncio.TimeoutError):
                        reason = "no_connect" if phase == "connect" else "no_reply"
                    else:
                        reason = "closed"
                    await self.close()
                    if patient and isinstance(err, asyncio.TimeoutError):
                        break      # it had the long wait on a fresh connection: asking again would only double the wait
            if body is None:
                raise ModbusError(f"{self.host}:{self.port} unreachable: {last!r}", reason=reason)
        if body[0] & 0x80:
            code = body[1] if len(body) > 1 else 0
            raise ModbusError(f"device returned exception {code}", code=code)
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
