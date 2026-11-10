"""Any Modbus TCP battery described by a register map (see core/modbus_map.py)."""
from __future__ import annotations

import logging

from ..core.modbus_map import has_control, parse_map
from ..core.models import Actual, Desired, DeviceConfig, Mode
from .modbus import ModbusError, ModbusTcpClient, from_i32, to_i32, to_u32

_LOGGER = logging.getLogger(__name__)


def _decode(regs: list[int], typ: str, order: str) -> int:
    if typ == "u16":
        return regs[0]
    if typ == "i16":
        return regs[0] - 0x10000 if regs[0] & 0x8000 else regs[0]
    return to_i32(regs[0], regs[1], order) if typ == "i32" else to_u32(regs[0], regs[1], order)


def _encode(value: int, typ: str, order: str) -> list[int]:
    if typ in ("u16", "i16"):
        return [value & 0xFFFF]
    return from_i32(value, order)


class GenericModbusDriver:
    def __init__(self, config: DeviceConfig, client: ModbusTcpClient | None = None) -> None:
        self.config = config
        c = config.extra["connection"]
        self.map = parse_map(c["map"])
        self.client = client or ModbusTcpClient(c["host"], int(c.get("port", 502)), int(c.get("unit_id", 1)))

    async def close(self) -> None:
        await self.client.close()

    async def _value(self, reg: dict | None) -> float | None:
        if not reg:
            return None
        total = 0.0
        for addr in reg["addrs"]:
            n = 2 if reg["type"] in ("u32", "i32") else 1
            fn = self.client.read_input if reg["fc"] == 4 else self.client.read_holding
            total += _decode(await fn(addr, n), reg["type"], self.map["word_order"]) * reg["scale"]
        return total

    async def _snapshot(self) -> dict:
        m = self.map
        soc, power = await self._value(m["soc"]), await self._value(m["power"])
        if m["power"]["positive_is"] == "discharging" and power is not None:
            power = -power                       # normalise: + charging
        grid = await self._value(m["grid"])
        if grid is not None and m["grid"]["positive_is"] == "export":
            grid = -grid                         # normalise: + importing
        return {"soc": soc, "power_w": power, "grid_w": grid, "pv_w": await self._value(m["pv"]),
                "load_w": await self._value(m["load"])}

    async def _control_state(self) -> tuple[Mode, int | None]:
        m = self.map
        mode_raw = int(await self._value(m["mode"]) or 0)
        if mode_raw != m["mode"]["control_value"]:
            return Mode.AUTO, None
        sp = await self._value(m["setpoint"]) or 0
        if m["setpoint"]["positive_is"] == "charge":
            sp = -sp
        if abs(sp) < 1:
            return Mode.HOLD, 0
        return (Mode.DISCHARGE if sp > 0 else Mode.CHARGE), int(abs(sp))

    async def read(self) -> Actual:
        try:
            s = await self._snapshot()
            mode, set_w = (await self._control_state()) if has_control(self.map) else (Mode.AUTO, None)
            return Actual(online=True, mode=mode, set_power_w=set_w, **s)
        except (ModbusError, IndexError) as err:
            _LOGGER.debug("%s read failed: %s", self.config.device_id, err)
            return Actual(online=False)

    async def probe(self) -> dict:
        s = await self._snapshot()
        return {**s, "mode": None, "model": None, "serial": None, "capacity_wh": None,
                "max_charge_w": None, "max_discharge_w": None, "min_soc": None, "control": has_control(self.map)}

    async def apply(self, desired: Desired) -> None:
        m = self.map
        if not has_control(m):
            if desired.mode is Mode.AUTO:
                return
            raise ValueError("this register map is read-only")
        sp_reg, mode_reg, order = m["setpoint"], m["mode"], m["word_order"]

        async def write(reg: dict, value: float) -> None:
            raw = int(round(value / reg["scale"]))
            await self.client.write_multiple(reg["addrs"][0], _encode(raw, reg["type"], order))

        if desired.mode is Mode.AUTO:
            await write(sp_reg, 0)
            await write(mode_reg, mode_reg["auto_value"] * mode_reg["scale"])
            return
        await write(mode_reg, mode_reg["control_value"] * mode_reg["scale"])
        watts = 0 if desired.mode is Mode.HOLD else int(desired.power_w if desired.power_w is not None else (
            self.config.caps.max_charge_w if desired.mode is Mode.CHARGE else self.config.caps.max_discharge_w))
        signed = watts if desired.mode is Mode.DISCHARGE else -watts if desired.mode is Mode.CHARGE else 0
        await write(sp_reg, -signed if sp_reg["positive_is"] == "charge" else signed)
