"""EcoFlow STREAM 5000 / STREAM AC 5000 over the device's own local Modbus TCP ("third-party control").

The register map comes from EcoFlow's official Home Assistant integration (EcoFlow-oasis-official/ha-EcoFlow-official, MIT);
how to switch it on comes from the EcoFlow app: STREAM system > Energy management > Device integration > Third-party control
> Modbus TCP (off by default). Unlike the cloud API this gives a real power setpoint, readings every few seconds and no rate
limit, so Charge and Discharge can be set in watts.

  * Addresses: register offset + 40001 on the wire (0x206 is read at 40519), unit id 1, port 502, words high first.
  * The device wants a keep-alive write (0x25F) at least once a minute; without it the device stops following us.
  * Control point 0x1610: 0 = the device's own energy management, 1 = we set the battery's power (0x1611, signed, watts).

Not verified on hardware: which sign 0x1611 uses. EcoFlow reports battery power as + charging, so the same is assumed here;
the connection option "reverse_sign" flips both if the Test control result shows the opposite.
"""
from __future__ import annotations

import logging
import struct
import time

from ..core.models import Actual, Desired, DeviceConfig, Mode
from .modbus import ModbusError, ModbusTcpClient, to_text

_LOGGER = logging.getLogger(__name__)

ADDRESS_OFFSET = 40001
REG_SERIAL, REG_FIRMWARE = 0x0003, 0x000B
REG_FLOW = 0x0206                 # consumption, grid, PV, battery power (float x2 each), then SOC
FLOW_COUNT = 9
REG_MAX_OUT, REG_MAX_IN, REG_CAPACITY = 0x0221, 0x0223, 0x0227
REG_SOC_LOW = 0x2200
REG_HEARTBEAT = 0x025F
REG_CONTROL_POINT, REG_BATTERY_TARGET = 0x1610, 0x1611
POINT_NONE, POINT_BATTERY = 0, 1
HEARTBEAT_EVERY_S = 20.0
MODELS = {"ES21": "STREAM 5000", "ES22": "STREAM AC 5000"}


def _float(a: int, b: int) -> float:
    return struct.unpack(">f", struct.pack(">HH", a & 0xFFFF, b & 0xFFFF))[0]


def _signed16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def _clean(x: float | None) -> float | None:
    return x if x is not None and x == x and abs(x) < 1e7 else None   # NaN / absurd values read as "unknown"


class EcoFlowModbusDriver:
    def __init__(self, config: DeviceConfig, client: ModbusTcpClient | None = None) -> None:
        self.config = config
        c = config.extra["connection"]
        self.client = client or ModbusTcpClient(c["host"], int(c.get("port", 502)), int(c.get("unit_id", 1)))
        self.sign = -1 if c.get("reverse_sign") else 1
        self.last_quota: dict = {}
        self.last_error: str | None = None
        self._beat = 0.0

    async def close(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------------ helpers
    async def _read(self, offset: int, count: int) -> list[int]:
        return await self.client.read_holding(offset + ADDRESS_OFFSET, count)

    async def _write(self, offset: int, value: int) -> None:
        await self.client.write_register(offset + ADDRESS_OFFSET, value)

    async def _heartbeat(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._beat >= HEARTBEAT_EVERY_S:
            await self._write(REG_HEARTBEAT, 1)
            self._beat = now

    # ------------------------------------------------------------------ read
    async def read(self) -> Actual:
        try:
            await self._heartbeat()
            flow = await self._read(REG_FLOW, FLOW_COUNT)
        except ModbusError as err:
            self.last_error = str(err)
            _LOGGER.debug("%s read failed: %s", self.config.device_id, err)
            return Actual(online=False)
        self.last_error = None
        load, grid, pv, batt = (_clean(_float(flow[i], flow[i + 1])) for i in (0, 2, 4, 6))
        soc = float(flow[8]) if flow[8] <= 100 else None
        mode, set_w = Mode.AUTO, None
        try:
            point, target = await self._read(REG_CONTROL_POINT, 2)
            if point == POINT_BATTERY:                       # we are setting the battery's power
                t = _signed16(target) * self.sign
                mode = Mode.HOLD if t == 0 else Mode.CHARGE if t > 0 else Mode.DISCHARGE
                set_w = abs(t)
        except ModbusError:
            pass                                             # the control block is optional for reading
        self.last_quota = {"consumption_w": load, "grid_w": grid, "pv_w": pv, "battery_w": batt, "soc": soc}
        return Actual(online=True, soc=soc, power_w=None if batt is None else batt * self.sign, mode=mode, set_power_w=set_w,
                      grid_w=grid, pv_w=pv, load_w=load)

    async def probe(self) -> dict:
        try:
            flow = await self._read(REG_FLOW, FLOW_COUNT)
        except ModbusError as err:
            raise ModbusError(f"{err}. Is Modbus TCP switched on in the EcoFlow app (STREAM system > Energy management > "
                              "Third-party control)? It is off by default.") from err
        load, grid, pv, batt = (_clean(_float(flow[i], flow[i + 1])) for i in (0, 2, 4, 6))
        info: dict = {"soc": float(flow[8]) if flow[8] <= 100 else None, "power_w": None if batt is None else batt * self.sign,
                      "grid_w": grid, "pv_w": pv, "load_w": load, "mode": None, "model": None, "serial": None, "firmware": None,
                      "capacity_wh": None, "max_charge_w": None, "max_discharge_w": None, "min_soc": None, "control": True}
        for key, offset, count, fn in (
                ("serial", REG_SERIAL, 8, lambda r: to_text(r) or None),
                ("capacity_wh", REG_CAPACITY, 2, lambda r: int((r[0] << 16) | r[1]) or None),
                ("max_discharge_w", REG_MAX_OUT, 2, lambda r: int((r[0] << 16) | r[1]) or None),
                ("max_charge_w", REG_MAX_IN, 2, lambda r: int((r[0] << 16) | r[1]) or None),
                ("min_soc", REG_SOC_LOW, 1, lambda r: int(r[0]) if r[0] <= 100 else None),
                ("firmware", REG_FIRMWARE, 2, lambda r: ".".join(str(b) for b in struct.pack(">I", (r[0] << 16) | r[1])))):
            try:
                info[key] = fn(await self._read(offset, count))
            except ModbusError:
                pass                                          # not every unit answers every register
        sn = (info["serial"] or "").upper()
        info["model"] = next((m for p, m in MODELS.items() if sn.startswith(p)), None)
        return info

    # ------------------------------------------------------------------ write
    async def keepalive(self) -> None:
        await self._heartbeat(force=True)

    async def apply(self, desired: Desired) -> None:
        caps = self.config.caps
        await self._heartbeat(force=True)
        point, _ = await self._read(REG_CONTROL_POINT, 2)
        if desired.mode is Mode.AUTO:
            if point != POINT_NONE:
                await self._write(REG_BATTERY_TARGET, 0)
                await self._write(REG_CONTROL_POINT, POINT_NONE)       # back to the device's own energy management
            return
        if desired.mode is Mode.HOLD:
            watts = 0
        elif desired.mode in (Mode.CHARGE, Mode.DISCHARGE):
            limit = caps.max_charge_w if desired.mode is Mode.CHARGE else caps.max_discharge_w
            watts = int(max(0, min(desired.power_w if desired.power_w is not None else limit, limit, 32000)))
        else:
            raise ValueError(f"cannot {desired.mode.value}")
        signed = (watts if desired.mode is Mode.CHARGE else -watts) * self.sign
        await self._write(REG_BATTERY_TARGET, signed & 0xFFFF)         # target first, so taking control never uses a stale one
        if point != POINT_BATTERY:
            await self._write(REG_CONTROL_POINT, POINT_BATTERY)
