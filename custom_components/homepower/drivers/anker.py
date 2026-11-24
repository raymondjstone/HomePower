"""Anker SOLIX over local Modbus TCP: Solarbank Max / Max AC, XE / XE AC, Solarbank 4 E5000 Pro, X1.

X1: Anker's own "X1 Series Modbus Protocol" V1.0.0 (see core/anker_x1_map.py) is the reference. On the X1, 32-bit
values come LOW word first and text registers hold their characters low byte first; both are handled below.
Register map (from the vendors' published Modbus protocol as used by the open-source integrations):
  input regs (read FC04):  10001 battery status | 10002 PV W (i32) | 10008 battery W (i32, negative =
                           charging) | 10010 load W | 10012 grid W (+ importing) | 10014 SOC %
                           10036 max charge W | 10038 max discharge W | 10250 rated energy (x0.1 kWh)
                           10100 serial | 32768 model
  holding regs (FC03/06/16): 10064 work mode (3 = third-party/VPP control) | 10071 battery setpoint
                           (i32, negative = charge, positive = discharge) | 10080 comms watchdog (s, X1)
32-bit values are two registers; the word order is detected from the device's own readings (the
units seen so far send the HIGH word first).
"""
from __future__ import annotations

import logging

from ..core.models import Actual, Desired, DeviceConfig, Mode
import time

from ..core.anker_x1_map import alarm_texts
from .modbus import ModbusError, ModbusTcpClient, describe_failure, known_down, detect_word_order, from_i32, to_i32, to_text, to_text_low_first, to_u32

_LOGGER = logging.getLogger(__name__)

REG_BASE, REG_COUNT = 10000, 51          # one read covers 10000..10050
REG_HOLD_BASE, REG_HOLD_COUNT = 10060, 13  # 10060..10072
MODE_REG, SETPOINT_REG, WATCHDOG_REG = 10064, 10071, 10080
MODE_THIRD_PARTY = 3
WATCHDOG_SECONDS = 600


PAIRS = (10002, 10008, 10010, 10012, 10036, 10038)  # 32-bit power registers used to detect word order


class AnkerModbusDriver:
    def __init__(self, config: DeviceConfig, client: ModbusTcpClient | None = None, orig: dict | None = None, on_orig=None) -> None:
        self.config = config
        c = config.extra["connection"]
        self.client = client or ModbusTcpClient(c["host"], int(c.get("port", 502)), int(c.get("unit_id", 1)))
        self._on_orig = on_orig                        # called when the remembered mode changes, so it survives a restart or an edit
        self._restore_val: int | None = int(orig["restore"]) if isinstance(orig, dict) and isinstance(orig.get("restore"), int) else None
        self._order: str | None = None    # "big" / "little" once detected
        self._slow_at = 0.0               # X1: next time to refresh the slowly-changing extras (temperature)
        self._firmware: str | None = None
        self._temp: float | None = None
        self._limits: tuple | None = None
        self._pack_v: tuple = ()
        self._packs_at = 0.0
        self._pv: tuple = ()                   # X1: ((V, A) x 8) from 10167-10182
        self._pv_total: float | None = None    # X1: total PV power, 10183
        self._pv_at = 0.0
        self.last_error: str | None = None     # why the last read failed, in plain words (None while it answers)
        self.outages: list[dict] = []          # the last few times it stopped answering: {"at": when it went quiet, "seconds": for how long}
        self._down_since: float | None = None
        self._down_wall = 0.0

    @property
    def _restore(self) -> int | None:
        """The mode the device was in before we took control (saved, so a restart or an edit does not lose it)."""
        return self._restore_val

    @_restore.setter
    def _restore(self, value: int | None) -> None:
        self._restore_val = value
        if self._on_orig is not None:
            try:
                self._on_orig({"restore": value} if value is not None else None)
            except Exception:  # noqa: BLE001 - failing to save must never stop a command
                _LOGGER.debug("could not save the original mode", exc_info=True)

    async def close(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------------ read
    async def _blocks(self) -> tuple[list[int], list[int]]:
        return (await self.client.read_input(REG_BASE, REG_COUNT),
                await self.client.read_holding(REG_HOLD_BASE, REG_HOLD_COUNT))

    def _i32(self, regs: list[int], addr: int, base: int = REG_BASE) -> int:
        i = addr - base
        return to_i32(regs[i], regs[i + 1], self._order or "big")

    def _learn_order(self, inp: list[int], hold: list[int]) -> None:
        if self._order is None:
            pairs = [(inp[a - REG_BASE], inp[a - REG_BASE + 1]) for a in PAIRS]
            pairs.append((hold[SETPOINT_REG - REG_HOLD_BASE], hold[SETPOINT_REG - REG_HOLD_BASE + 1]))
            self._order = detect_word_order(pairs)

    def _decode(self, inp: list[int], hold: list[int]) -> Actual:
        self._learn_order(inp, hold)
        mode_raw = hold[MODE_REG - REG_HOLD_BASE]
        sp = self._i32(hold, SETPOINT_REG, REG_HOLD_BASE)
        if mode_raw != MODE_THIRD_PARTY:
            mode, set_w = Mode.AUTO, None
        elif sp == 0:
            mode, set_w = Mode.HOLD, 0
        else:
            mode, set_w = (Mode.DISCHARGE if sp > 0 else Mode.CHARGE), abs(sp)
        # X1: plant status 1 = on-grid, 2 = off-grid. While the grid is lost the inverter runs the house
        # itself, so HomePower must not fight it (protected makes the engine leave it alone).
        x1 = bool(self.config.extra.get("house_meter"))
        alarms = tuple(alarm_texts(inp[42:50])) if x1 else ()      # System Alarm 1-8 (10042-10049), all inside the block read
        plant = inp[0] if x1 else None
        off_grid = (plant == 2) if plant in (1, 2) else None
        return Actual(
            online=True, off_grid=off_grid, protected=bool(off_grid), alarms=alarms, firmware=self._firmware, temp_c=self._temp, limits=self._limits, pack_v=self._pack_v,
            pv_strings=self._pv, pv_total_w=self._pv_total,
            soc=float(inp[14]),
            power_w=float(-self._i32(inp, 10008)),  # register: negative = charging; we use + charging
            mode=mode, set_power_w=set_w,
            grid_w=float(self._i32(inp, 10012)), pv_w=float(self._i32(inp, 10002)), load_w=float(self._i32(inp, 10010)),
        )

    async def _refresh_extras(self) -> None:
        """X1: internal temperature (10156, INT16, tenths of a degree) every minute and the software version
        (10112, text) every few hours. Failures are ignored: they are extras, not needed for control."""
        now = time.monotonic()
        if now < self._slow_at:
            return
        self._slow_at = now + 60
        if not self.config.extra.get("house_meter"):
            try:   # Solarbank: its own charge / discharge limits and backup reserve (holding 60000-60003, Anker's register file)
                lim = await self.client.read_holding(60000, 4)
                self._limits = (lim[0], lim[1], lim[2], bool(lim[3]))
            except ModbusError:
                self._limits = None
            return
        try:
            t = (await self.client.read_input(10156, 1))[0]
            self._temp = round((t - 0x10000 if t & 0x8000 else t) / 10, 1)
        except ModbusError as err:
            if err.code is None:
                return          # it is not answering at all: the other extras would only wait as long again
        if now >= self._packs_at:      # battery pack voltages every 5 minutes (pack n voltage = 10321 + 56 (n-1))
            self._packs_at = now + 300
            volts = []
            for n in range(6):
                try:
                    v = (await self.client.read_input(10321 + 56 * n, 1))[0]
                except ModbusError:
                    break
                if v:
                    volts.append(v)
            self._pack_v = tuple(volts)
        if self._firmware is None or now - getattr(self, "_fw_at", 0) > 6 * 3600:
            try:
                self._firmware = to_text_low_first(await self.client.read_input(10112, 6)) or None
                self._fw_at = now
            except ModbusError:
                pass

    async def _refresh_pv(self) -> None:
        """X1: each PV string's voltage (0.1 V) and current (0.01 A), 10167-10182, and the total PV power (10183, 32-bit),
        at most every 15 s. A failure keeps the last values; they are extras, not needed for control."""
        now = time.monotonic()
        if now < self._pv_at or not self.config.extra.get("house_meter"):
            return
        self._pv_at = now + 15
        try:
            regs = await self.client.read_input(10167, 18)
        except ModbusError:
            return

        def s16(w: int) -> int:
            return w - 0x10000 if w & 0x8000 else w
        self._pv = tuple((max(0, s16(regs[2 * i])) / 10, max(0, s16(regs[2 * i + 1])) / 100) for i in range(8))
        self._pv_total = float(max(0, to_i32(regs[16], regs[17], self._order or "big")))

    def _note_down(self, err: ModbusError) -> None:
        self.last_error = describe_failure(err)
        if self._down_since is None:
            self._down_since, self._down_wall = time.monotonic(), time.time()
            _LOGGER.warning("%s: the battery stopped answering: %s", self.config.device_id, self.last_error)

    def _note_up(self) -> None:
        if self._down_since is not None:
            seconds = round(time.monotonic() - self._down_since)
            self.outages = (self.outages + [{"at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._down_wall)), "seconds": seconds}])[-10:]
            _LOGGER.info("%s: the battery answers again after %d s", self.config.device_id, seconds)
            self._down_since = None
        self.last_error = None

    async def read(self) -> Actual:
        try:
            await self._refresh_extras()
            await self._refresh_pv()
            actual = self._decode(*await self._blocks())
        except ModbusError as err:
            _LOGGER.debug("%s read failed: %s", self.config.device_id, err)
            self._note_down(err)
            return Actual(online=False, gone=known_down(err))
        self._note_up()
        return actual

    async def probe(self) -> dict:
        """Everything useful for setup, read from the battery itself: live values, model, capacity,
        power limits and its configured minimum charge. Never writes."""
        inp, hold = await self._blocks()
        a = self._decode(inp, hold)
        info: dict = {
            "soc": a.soc, "power_w": a.power_w, "mode": a.mode.value if a.mode else None,
            "grid_w": a.grid_w, "pv_w": a.pv_w, "load_w": a.load_w, "word_order": self._order,
            "max_charge_w": self._i32(inp, 10036) or None, "max_discharge_w": self._i32(inp, 10038) or None,
            "min_soc": None,
        }
        x1 = bool(self.config.extra.get("house_meter"))
        for key, addr, count in (("serial", 10100, 8), ("model", 10090 if x1 else 32768, 10 if x1 else 16)):
            try:
                regs = await self.client.read_input(addr, count)
                info[key] = (to_text_low_first(regs) if x1 else to_text(regs)) or None
            except ModbusError:
                info[key] = None
        if x1:
            info["firmware"] = self._firmware = a.firmware or None
            try:
                info["firmware"] = self._firmware = to_text_low_first(await self.client.read_input(10112, 6)) or None
            except ModbusError:
                pass
        try:
            rated = to_u32(*await self.client.read_input(10250, 2), self._order or "big")
            info["capacity_wh"] = int(rated * 100) or None  # register is in 0.1 kWh
        except ModbusError:
            info["capacity_wh"] = None
        try:  # the battery's own discharge floor (not present on every model)
            info["min_soc"] = (await self.client.read_holding(60001, 1))[0]
        except ModbusError:
            pass
        return info

    async def set_grid_limits(self, import_w: int | None, export_w: int | None, mine: dict[str, int] | None = None) -> list[str]:
        """X1 only: mirror HomePower's house import / export limit into the inverter's own limit registers
        (10074 export mode, 10075-76 value, 10077 import mode, 10078-79 value; mode 0 off, 2 fixed watts), so the
        limit also holds if Home Assistant is down. Cautious by design: a limit HomePower did not write (an installer's
        grid-code limit) is only ever tightened, never raised or switched off; nothing that is already right is written.
        `mine` holds the limits HomePower itself wrote ({"export": W, "import": W}, kept across restarts by the caller and
        updated here): one of those still in place follows HomePower's setting up as well as down, and is switched off
        again when HomePower's limit is cleared. Returns what it changed."""
        if self._order is None:
            return []             # the 32-bit word order is not known yet: never write a number that could be misread
        mine = mine if mine is not None else {}
        hold = await self.client.read_holding(10074, 6)
        order, changed = self._order, []
        for label, base, want in (("export", 0, export_w), ("import", 3, import_w)):
            mode, cur = hold[base], to_u32(hold[base + 1], hold[base + 2], order)
            ours = mode == 2 and mine.get(label) == cur          # still the value HomePower wrote: nobody has changed it since
            if mode == 2 and mine.get(label) is not None and not ours:
                mine.pop(label, None)                            # changed in the app or by an installer since: no longer ours
            if not want or want <= 0:
                if ours:                                         # HomePower's limit was cleared: take ours off again
                    await self.client.write_register(10074 + base, 0)
                    mine.pop(label, None)
                    changed.append(f"{label} limit off")
                continue
            if mode == 2 and cur == want:
                continue
            if mode != 0 and cur and cur < want and not ours:
                continue          # the inverter already has a stricter limit someone else set: leave it alone
            await self.client.write_multiple(10074 + base + 1, from_i32(int(want), order))
            await self.client.write_register(10074 + base, 2)
            mine[label] = int(want)
            changed.append(f"{label} limit {want} W")
        return changed

    async def scan(self, ranges=((10000, 10700), (60000, 60100))) -> dict[str, dict[str, int]]:
        """Read-only dump of raw registers (holding and input) for finding which register an app setting lives in:
        scan, change one setting in the vendor app, scan again and compare. Never writes. A device that does not
        answer a range at all (some stay silent instead of returning an error) is skipped after a few tries."""
        out: dict[str, dict[str, int]] = {"holding": {}, "input": {}}
        for kind, read in (("holding", self.client.read_holding), ("input", self.client.read_input)):
            for lo, hi in ranges:
                addr, silent = lo, 0
                while addr <= hi and silent < 3:
                    n = min(10, hi - addr + 1)
                    try:
                        vals = await read(addr, n)
                        out[kind].update({str(addr + i): v for i, v in enumerate(vals)})
                        silent = 0
                    except ModbusError as err:
                        if err.code is None:      # no reply at all
                            silent += 1
                        else:                     # the device refused this block: try its registers one by one
                            for a in range(addr, addr + n):
                                try:
                                    out[kind][str(a)] = (await read(a, 1))[0]
                                except ModbusError:
                                    pass
                    addr += n
        return out

    async def read_registers(self, addrs: list[int]) -> dict[str, int]:
        """Read-only: the current value of the given registers (the holding copy, or the input copy if refused).
        Nearby addresses are read together; a block the device refuses is retried register by register."""
        out: dict[str, int] = {}
        todo = sorted(set(int(a) for a in addrs))[:64]
        runs: list[list[int]] = []
        for a in todo:
            if runs and a - runs[-1][-1] <= 3 and a - runs[-1][0] < 20:
                runs[-1].append(a)
            else:
                runs.append([a])
        silent = 0
        for run in runs:
            if silent >= 3:        # the device ignores these addresses: stop waiting on each one (the connection is shared)
                break
            left = list(run)
            for read in (self.client.read_holding, self.client.read_input):
                if not left:
                    break
                try:
                    vals = await read(left[0], left[-1] - left[0] + 1)
                    out.update({str(left[0] + i): v for i, v in enumerate(vals) if left[0] + i in left})
                    left = []
                except ModbusError as err:
                    if err.code is None:       # no reply: try the other kind of register
                        silent += 1
                        continue
                    for a in list(left):       # refused as a block: one by one
                        try:
                            out[str(a)] = (await read(a, 1))[0]
                            left.remove(a)
                        except ModbusError:
                            pass
        return out

    async def keepalive(self) -> None:
        """Re-send the current setpoint unchanged so the device's comms watchdog (X1) stays fed."""
        hold = await self.client.read_holding(REG_HOLD_BASE, REG_HOLD_COUNT)
        if hold[MODE_REG - REG_HOLD_BASE] == MODE_THIRD_PARTY:
            i = SETPOINT_REG - REG_HOLD_BASE
            await self.client.write_multiple(SETPOINT_REG, hold[i:i + 2])

    # ----------------------------------------------------------------- write
    async def _set_setpoint(self, watts: int) -> None:
        await self.client.write_multiple(SETPOINT_REG, from_i32(watts, self._order or "big"))

    async def apply(self, desired: Desired) -> None:
        caps, extra = self.config.caps, self.config.extra
        hold = await self.client.read_holding(REG_HOLD_BASE, REG_HOLD_COUNT)
        cur_mode = hold[MODE_REG - REG_HOLD_BASE]

        if desired.mode in (Mode.CHARGE, Mode.DISCHARGE):
            if self._order is None:   # learn the 32-bit word order before the first non-zero setpoint
                self._learn_order(await self.client.read_input(REG_BASE, REG_COUNT), hold)
            if self._order is None:
                raise ModbusError("the battery's 32-bit word order is not known yet (all readings are zero), "
                                  "so no setpoint was sent; it is learned automatically once the battery reports power")
            limit = caps.max_charge_w if desired.mode is Mode.CHARGE else caps.max_discharge_w
            watts = desired.power_w if desired.power_w is not None else limit
            desired = Desired(desired.mode, int(max(0, min(watts, limit))), desired.target_soc, desired.reserve_soc)

        if desired.mode is Mode.AUTO:
            if cur_mode == MODE_THIRD_PARTY:
                await self._set_setpoint(0)
                target = self._restore if self._restore not in (None, MODE_THIRD_PARTY) else int(extra.get("restore_mode", 0))
                await self.client.write_register(MODE_REG, target)
                self._restore = None
            return

        if cur_mode != MODE_THIRD_PARTY:  # taking control: remember what to hand back to
            self._restore = cur_mode
            await self.client.write_register(MODE_REG, MODE_THIRD_PARTY)
            if extra.get("watchdog"):
                try:
                    await self.client.write_register(WATCHDOG_REG, WATCHDOG_SECONDS)
                except ModbusError as err:  # non-fatal: the safety net is optional
                    _LOGGER.warning("%s: could not set comms watchdog: %s", self.config.device_id, err)

        if desired.mode is Mode.HOLD:
            await self._set_setpoint(0)
        elif desired.mode is Mode.CHARGE:
            await self._set_setpoint(-int(desired.power_w if desired.power_w is not None else caps.max_charge_w))
        else:
            await self._set_setpoint(int(desired.power_w if desired.power_w is not None else caps.max_discharge_w))
