"""Read-only Modbus register scanner, for finding which register an app setting lives in.

Never writes. Two steps:
  1. python tools/modbus_scan.py scan 192.168.1.50 before.json
  2. change ONE setting in the Anker app (e.g. grid-off sensitivity), then:
     python tools/modbus_scan.py scan 192.168.1.50 after.json
  3. python tools/modbus_scan.py diff before.json after.json
Live readings (power, SOC, grid) change on their own, so a register that only ever differs by a
little between two scans with nothing changed is noise. Run `scan` twice with no change first and
`diff` those to see which registers are noisy, then ignore them.

Options for scan: --port 502 --unit 1 --ranges 10000-10300,32760-32800,60000-60100
Registers the device refuses are skipped; each is read in small blocks so one bad address
doesn't hide its neighbours.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import pathlib

_spec = importlib.util.spec_from_file_location(
    "modbus", pathlib.Path(__file__).parent.parent / "custom_components" / "homepower" / "drivers" / "modbus.py")
modbus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(modbus)

DEFAULT_RANGES = "10000-10300,32760-32800,60000-60100"
BLOCK = 10


async def _scan_kind(read, lo: int, hi: int) -> dict[int, int]:
    """Some devices answer an unsupported address with an error, others just stay silent (timeout).
    Either way the block is skipped; a silent block is not retried register by register (too slow)."""
    out: dict[int, int] = {}
    addr, silent = lo, 0
    while addr <= hi:
        n = min(BLOCK, hi - addr + 1)
        try:
            vals = await read(addr, n)
            out.update({addr + i: v for i, v in enumerate(vals)})
            silent = 0
        except modbus.ModbusError as err:
            if "unreachable" in str(err):
                silent += 1
                if silent >= 5:
                    print(f"  no answer from {addr - 4 * BLOCK} onwards, skipping the rest of this range")
                    break
            elif n > 1:   # an error reply: retry one by one so one bad register doesn't hide its neighbours
                for a in range(addr, addr + n):
                    try:
                        out[a] = (await read(a, 1))[0]
                    except modbus.ModbusError:
                        pass
        addr += n
    return out


async def scan(args) -> None:
    c = modbus.ModbusTcpClient(args.host, args.port, args.unit, timeout=args.timeout)
    result: dict[str, dict[str, int]] = {"holding": {}, "input": {}}
    try:
        for part in args.ranges.split(","):
            lo, hi = (int(x) for x in part.split("-"))
            for kind, read in (("holding", c.read_holding), ("input", c.read_input)):
                got = await _scan_kind(read, lo, hi)
                result[kind].update({str(k): v for k, v in got.items()})
                print(f"{kind:8} {lo}-{hi}: {len(got)} readable")
    finally:
        await c.close()
    pathlib.Path(args.out).write_text(json.dumps(result, indent=1))
    print(f"saved {args.out}")


def diff(args) -> None:
    a = json.loads(pathlib.Path(args.before).read_text())
    b = json.loads(pathlib.Path(args.after).read_text())
    changed = missing = 0
    for kind in ("holding", "input"):
        for addr in sorted(set(a[kind]) | set(b[kind]), key=int):
            x, y = a[kind].get(addr), b[kind].get(addr)
            if x is None or y is None:   # one scan didn't get an answer: can't compare
                missing += 1
            elif x != y:
                changed += 1
                print(f"{kind:8} {addr}: {x} -> {y}")
    print(f"{changed} register(s) differ")
    if missing:
        print(f"{missing} register(s) were read in only one of the two scans and were not compared")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("host"); s.add_argument("out")
    s.add_argument("--port", type=int, default=502); s.add_argument("--unit", type=int, default=1)
    s.add_argument("--ranges", default=DEFAULT_RANGES)
    s.add_argument("--timeout", type=float, default=3.0, help="seconds to wait per request (default 3)")
    d = sub.add_parser("diff")
    d.add_argument("before"); d.add_argument("after")
    args = p.parse_args()
    asyncio.run(scan(args)) if args.cmd == "scan" else diff(args)


if __name__ == "__main__":
    main()
