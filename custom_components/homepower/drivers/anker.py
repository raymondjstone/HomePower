"""Anker SOLIX over local Modbus TCP: Solarbank Max / Max AC, XE / XE AC, Solarbank 4 E5000 Pro, X1.

Register map (from the vendors' published Modbus protocol as used by the open-source integrations):
  input regs (read FC04):  10001 battery status | 10002 PV W (i32) | 10008 battery W (i32, negative =
                           charging) | 10010 load W | 10012 grid W (+ importing) | 10014 SOC %
                           10036 max charge W | 10038 max discharge W | 10250 rated energy (x0.1 kWh)
                           10100 serial | 32768 model
  holding regs (FC03/06/16): 10064 work mode (3 = third-party/VPP control) | 10071 battery setpoint
                           (i32, negative = charge, positive = discharge) | 10080 comms watchdog (s, X1)
32-bit values are sent LOW word first.
"""
from __future__ import annotations

import logging

from ..core.models import Actual, Desired, DeviceConfig, Mode
from .modbus import ModbusError, ModbusTcpClient, from_i32, to_i32, to_text, to_u32

_LOGGER = logging.getLogger(__name__)

REG_BASE, REG_COUNT = 10000, 51          # one read covers 10000..10050
REG_HOLD_BASE, REG_HOLD_COUNT = 10060, 13  # 10060..10072
MODE_REG, SETPOINT_REG, WATCHDOG_REG = 10064, 10071, 10080
MODE_THIRD_PARTY = 3
WATCHDOG_SECONDS = 600


def _i32(regs: list[int], addr: int) -> int:
    i = addr - REG_BASE
    return to_i32(regs[i], regs[i + 1])


class AnkerModbusDriver:
    def __init__(self, config: DeviceConfig, client: ModbusTcpClient | None = None) -> None:
        self.config = config
        c = config.extra["connection"]
        self.client = client or ModbusTcpClient(c["host"], int(c.get("port", 502)), int(c.get("unit_id", 1)))
        self._restore: int | None = None  # mode the device was in before we took control

    async def close(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------------ read
    async def _blocks(self) -> tuple[list[int], list[int]]:
        return (await self.client.read_input(REG_BASE, REG_COUNT),
                await self.client.read_holding(REG_HOLD_BASE, REG_HOLD_COUNT))

    @staticmethod
    def _decode(inp: list[int], hold: list[int]) -> Actual:
        mode_raw = hold[MODE_REG - REG_HOLD_BASE]
        sp = to_i32(hold[SETPOINT_REG - REG_HOLD_BASE], hold[SETPOINT_REG - REG_HOLD_BASE + 1])
        if mode_raw != MODE_THIRD_PARTY:
            mode, set_w = Mode.AUTO, None
        elif sp == 0:
            mode, set_w = Mode.HOLD, 0
        else:
            mode, set_w = (Mode.DISCHARGE if sp > 0 else Mode.CHARGE), abs(sp)
        return Actual(
            online=True,
            soc=float(inp[14]),
            power_w=float(-_i32(inp, 10008)),  # register: negative = charging; we use + charging
            mode=mode, set_power_w=set_w,
            grid_w=float(_i32(inp, 10012)), pv_w=float(_i32(inp, 10002)), load_w=float(_i32(inp, 10010)),
        )

    async def read(self) -> Actual:
        try:
            return self._decode(*await self._blocks())
        except ModbusError as err:
            _LOGGER.debug("%s read failed: %s", self.config.device_id, err)
            return Actual(online=False)

    async def probe(self) -> dict:
        """Everything useful for the setup screen: live values plus what the device says it is."""
        inp, hold = await self._blocks()
        a = self._decode(inp, hold)
        info: dict = {
            "soc": a.soc, "power_w": a.power_w, "mode": a.mode.value if a.mode else None,
            "grid_w": a.grid_w, "pv_w": a.pv_w, "load_w": a.load_w,
            "max_charge_w": _i32(inp, 10036) or None, "max_discharge_w": _i32(inp, 10038) or None,
        }
        for key, addr, count in (("serial", 10100, 8), ("model", 32768, 16)):
            try:
                info[key] = to_text(await self.client.read_input(addr, count)) or None
            except ModbusError:
                info[key] = None
        try:
            rated = to_u32(*await self.client.read_input(10250, 2))
            info["capacity_wh"] = int(rated * 100) or None  # register is in 0.1 kWh
        except ModbusError:
            info["capacity_wh"] = None
        return info

    # ----------------------------------------------------------------- write
    async def _set_setpoint(self, watts: int) -> None:
        await self.client.write_multiple(SETPOINT_REG, from_i32(watts))

    async def apply(self, desired: Desired) -> None:
        caps, extra = self.config.caps, self.config.extra
        hold = await self.client.read_holding(REG_HOLD_BASE, REG_HOLD_COUNT)
        cur_mode = hold[MODE_REG - REG_HOLD_BASE]

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
