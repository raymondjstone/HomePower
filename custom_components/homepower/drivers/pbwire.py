"""A very small protobuf wire-format reader/writer: just enough for the EcoFlow STREAM AC 5000's app protocol.

Reading never raises on odd input (it stops and returns what it has); a message is a {field number: [values]} dict where a
value is an int (varint), bytes (length-delimited) or a Fixed32/Fixed64 wrapper. No dependency on the protobuf package.
"""
from __future__ import annotations

import struct
from typing import Any


class Fixed32(int):
    """Wire type 5: raw 32 bits, readable as a float or an integer."""

    def as_float(self) -> float:
        return struct.unpack("<f", int(self).to_bytes(4, "little"))[0]


class Fixed64(int):
    def as_double(self) -> float:
        return struct.unpack("<d", int(self).to_bytes(8, "little"))[0]


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError("bad varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return result, pos


def parse(buf: bytes) -> dict[int, list[Any]]:
    out: dict[int, list[Any]] = {}
    pos = 0
    try:
        while pos < len(buf):
            tag, pos = _varint(buf, pos)
            field, wt = tag >> 3, tag & 7
            if field == 0:
                break
            if wt == 0:
                val, pos = _varint(buf, pos)
            elif wt == 2:
                n, pos = _varint(buf, pos)
                if pos + n > len(buf):
                    break
                val, pos = bytes(buf[pos:pos + n]), pos + n
            elif wt == 5:
                if pos + 4 > len(buf):
                    break
                val, pos = Fixed32(int.from_bytes(buf[pos:pos + 4], "little")), pos + 4
            elif wt == 1:
                if pos + 8 > len(buf):
                    break
                val, pos = Fixed64(int.from_bytes(buf[pos:pos + 8], "little")), pos + 8
            else:
                break
            out.setdefault(field, []).append(val)
    except ValueError:
        pass
    return out


def has(msg: dict, field: int) -> bool:
    return field in msg


def num(msg: dict, field: int) -> float | None:
    """The last value of a numeric field (a varint as an int, a Fixed32 as a float), or None if absent."""
    v = msg.get(field)
    if not v:
        return None
    v = v[-1]
    if isinstance(v, Fixed32):
        return v.as_float()
    if isinstance(v, Fixed64):
        return v.as_double()
    return v if isinstance(v, int) else None


def sub(msg: dict, field: int) -> dict:
    v = msg.get(field)
    return parse(v[-1]) if v and isinstance(v[-1], bytes) else {}


def text(msg: dict, field: int) -> str | None:
    v = msg.get(field)
    if v and isinstance(v[-1], bytes):
        try:
            return v[-1].decode()
        except UnicodeDecodeError:
            return None
    return None


# ---- writing
def enc_varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def enc_int(field: int, n: int) -> bytes:
    return enc_varint(field << 3) + enc_varint(n)


def enc_bytes(field: int, data: bytes | str) -> bytes:
    data = data.encode() if isinstance(data, str) else data
    return enc_varint(field << 3 | 2) + enc_varint(len(data)) + data
