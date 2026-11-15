"""Names for the Anker SOLIX X1's Modbus registers, from Anker's "X1 Series Modbus Protocol" V1.0.0 (2025-08-07),
https://salesforce-knowledge-download.s3.us-west-2.amazonaws.com/000029136/en_US/000029136.pdf

Used by the panel's register compare tool to name what changed and to tell live readings from settings. Only
registers whose address is unambiguous in the document and agrees with a real X1's readings are listed; anything
else is reported by number only. (HA-free.)

kind: "live"    a measurement that changes on its own (ignore it when looking for a changed setting)
      "setting" read/write: something that can be configured
      "info"    fixed identity / rating / status that rarely changes
      "alarm"   alarm bit-field
"""
from __future__ import annotations

def _pack(n: int) -> tuple[tuple[int, int, str, str, str], ...]:
    b = 10284 + 56 * (n - 1)
    return (
        (b, 10, f"Pack {n} model name", "RO", "info"), (b + 10, 12, f"Pack {n} serial number", "RO", "info"),
        (b + 22, 6, f"Pack {n} software version", "RO", "info"), (b + 28, 6, f"Pack {n} hardware version", "RO", "info"),
        (b + 34, 2, f"Pack {n} rated capacity", "RO", "info"),
        (b + 36, 1, f"Pack {n} status (1 charging, 2 discharging, 3 standby, 4 sleep, 5 correction, 6 heating)", "RO", "live"),
        (b + 37, 1, f"Pack {n} voltage", "RO", "live"), (b + 38, 2, f"Pack {n} power", "RO", "live"),
        (b + 40, 1, f"Pack {n} SOC", "RO", "live"), (b + 41, 1, f"Pack {n} SOH", "RO", "live"),
        (b + 42, 2, f"Pack {n} total discharge energy", "RO", "live"),
        (b + 44, 1, f"Pack {n} maximum temperature", "RO", "live"), (b + 45, 1, f"Pack {n} minimum temperature", "RO", "live"),
    )


_PACKS = tuple(r for n in range(1, 7) for r in _pack(n))

# (first address, number of registers, name, access, kind)
X1_REGISTERS: tuple[tuple[int, int, str, str, str], ...] = (
    (10000, 1, "Plant status (1 on-grid, 2 off-grid, 3 standby, 4 fault)", "RO", "info"),
    (10001, 1, "Battery status", "RO", "live"),
    (10002, 2, "PV power", "RO", "live"),
    (10004, 2, "3rd-party PV power", "RO", "live"),
    (10006, 2, "Active power (PCS AC side)", "RO", "live"),
    (10008, 2, "Battery power", "RO", "live"),
    (10010, 2, "Load power", "RO", "live"),
    (10012, 2, "Grid power", "RO", "live"),
    (10014, 1, "SOC", "RO", "live"),
    (10015, 1, "SOH", "RO", "live"),
    (10016, 2, "Daily PV generation", "RO", "live"),
    (10018, 2, "Total PV generation", "RO", "live"),
    (10020, 2, "Daily battery charge energy", "RO", "live"),
    (10022, 2, "Total battery charge energy", "RO", "live"),
    (10024, 2, "Daily load consumption", "RO", "live"),
    (10026, 2, "Total load consumption", "RO", "live"),
    (10028, 2, "Daily purchased energy", "RO", "live"),
    (10030, 2, "Total purchased energy", "RO", "live"),
    (10032, 2, "Daily feed-in energy", "RO", "live"),
    (10034, 2, "Total feed-in energy", "RO", "live"),
    (10036, 2, "Rechargeable power", "RO", "live"),
    (10038, 2, "Dischargeable power", "RO", "live"),
    (10040, 1, "Number of PCS", "RO", "info"),
    (10041, 1, "Number of available PCS", "RO", "info"),
    (10042, 8, "System alarms 1-8", "RO", "alarm"),
    (10060, 2, "System time", "RW", "live"),
    (10062, 1, "Time zone", "RW", "setting"),
    (10063, 1, "DST (1 enable, 2 disable)", "RW", "setting"),
    (10064, 1, "Work mode (0 self-consumption, 1 time-of-use, 2 backup only, 3 third-party control, 4 user-defined, 5 socket aggregation)", "RW", "setting"),
    (10069, 1, "Active power control (%)", "RW", "setting"),
    (10070, 1, "Reactive power control (%)", "RW", "setting"),
    (10071, 2, "Battery charge/discharge control (+ discharge, - charge; third-party mode only)", "RW", "setting"),
    (10073, 1, "Switch on/off (write only)", "WO", "setting"),
    (10074, 1, "Export power limit mode (0 off, 1 percent, 2 fixed W)", "RW", "setting"),
    (10075, 2, "Export power limit value", "RW", "setting"),
    (10077, 1, "Import power limit mode (0 off, 1 percent, 2 fixed W)", "RW", "setting"),
    (10078, 2, "Import power limit value", "RW", "setting"),
    (10080, 1, "Comms-disconnect time with VPP (s, default 1800)", "RW", "setting"),
    (10090, 10, "Model name", "RO", "info"),
    (10100, 12, "Serial number", "RO", "info"),
    (10112, 6, "Software version", "RO", "info"),
    (10118, 6, "Hardware version", "RO", "info"),
    (10124, 2, "Rated power", "RO", "info"),
    (10126, 2, "Maximum active power", "RO", "info"),
    (10128, 2, "Maximum apparent power", "RO", "info"),
    (10130, 1, "Number of MPPT", "RO", "info"),
    (10131, 1, "Strings per MPPT", "RO", "info"),
    (10132, 1, "Output mode (1 L/N, 2 L1/L2/N, 3 3P3W, 4 3P4W)", "RO", "info"),
    (10143, 1, "PCS work status (0 shutdown, 1 on-grid, 2 off-grid, 3 standby, 4 self-check, 5 fault)", "RO", "info"),
    (10144, 12, "PCS alarms 1-12", "RO", "alarm"),
    (10156, 1, "Internal temperature", "RO", "live"),
    (10167, 16, "PV1-PV8 voltage / current", "RO", "live"),
    (10183, 2, "Total PV power", "RO", "live"),
    (10185, 2, "Daily PV generation", "RO", "live"),
    (10187, 2, "Total PV generation", "RO", "live"),
    (10199, 6, "Grid voltage (Uab / Ubc / Uca / phase A, B, C)", "RO", "live"),
    (10205, 3, "Grid current (phase A, B, C)", "RO", "live"),
    (10208, 2, "Grid active power", "RO", "live"),
    (10210, 2, "Grid reactive power", "RO", "live"),
    (10212, 1, "Grid power factor", "RO", "live"),
    (10213, 1, "Grid frequency", "RO", "live"),
    (10224, 6, "Backup (EPS) voltage (Uab / Ubc / Uca / phase A, B, C)", "RO", "live"),
    (10230, 3, "Backup current (phase A, B, C)", "RO", "live"),
    (10233, 2, "Backup active power", "RO", "live"),
    (10235, 2, "Backup reactive power", "RO", "live"),
    (10237, 1, "Backup power factor", "RO", "live"),
    (10238, 1, "Backup frequency", "RO", "live"),
    (10249, 1, "Battery status (1 charging, 2 discharging, 3 standby, 4 sleep, 5 SOC/SOH correction)", "RO", "live"),
    (10250, 2, "Battery rated capacity", "RO", "info"),
    (10252, 1, "Battery voltage", "RO", "live"),
    (10254, 2, "Battery pack power (+ discharging, - charging)", "RO", "live"),
    (10256, 1, "Battery SOC", "RO", "live"),
    (10257, 1, "Battery SOH", "RO", "live"),
    (10258, 2, "Battery daily charge energy", "RO", "live"),
    (10260, 2, "Battery daily discharge energy", "RO", "live"),
    (10262, 2, "Battery total charge energy", "RO", "live"),
    (10264, 2, "Battery total discharge energy", "RO", "live"),
    (10266, 8, "Battery alarms 1-8", "RO", "alarm"),
    *_PACKS,
    (10620, 10, "Meter model", "RO", "info"),
    (10630, 1, "Meter type (1 single phase, 2 three phase)", "RO", "info"),
    (10631, 1, "Meter status (0 normal, 1 offline, 3 fault)", "RO", "info"),
    (10632, 3, "Meter voltage (phase A, B, C)", "RO", "live"),
    (10635, 3, "Meter current (phase A, B, C)", "RO", "live"),
    (10638, 2, "Meter phase A active power", "RO", "live"),
    (10640, 2, "Meter phase B active power", "RO", "live"),
    (10642, 2, "Meter phase C active power", "RO", "live"),
    (10644, 2, "Meter total active power (+ from grid, - to grid)", "RO", "live"),
    (10646, 2, "Meter total reactive power", "RO", "live"),
    (10648, 1, "Meter power factor", "RO", "live"),
    (10649, 1, "Meter grid frequency", "RO", "live"),
    (10650, 2, "Meter phase A forward energy", "RO", "live"),
    (10652, 2, "Meter phase B forward energy", "RO", "live"),
    (10654, 2, "Meter phase C forward energy", "RO", "live"),
    (10656, 2, "Meter total forward energy (bought from grid)", "RO", "live"),
    (10658, 2, "Meter phase A reverse energy", "RO", "live"),
    (10660, 2, "Meter phase B reverse energy", "RO", "live"),
    (10662, 2, "Meter phase C reverse energy", "RO", "live"),
    (10664, 2, "Meter total reverse energy (sold to grid)", "RO", "live"),
)

# Solarbank (E5000 Pro etc.): the same bank for the live readings, plus its own SOC limits. From Anker's official
# integration (anker-charging/ha-anker-solix-official, Solarbank 4 E5000 Pro register file).
SOLARBANK_REGISTERS: tuple[tuple[int, int, str, str, str], ...] = (
    (10001, 1, "Battery status (0 standby, 1 charging, 2 discharging, 3 sleep)", "RO", "live"),
    (10002, 2, "PV power", "RO", "live"),
    (10004, 2, "3rd-party PV power", "RO", "live"),
    (10008, 2, "Battery power", "RO", "live"),
    (10010, 2, "Load power", "RO", "live"),
    (10012, 2, "Grid power", "RO", "live"),
    (10014, 1, "SOC", "RO", "live"),
    (10018, 2, "PV generation, lifetime", "RO", "live"),
    (10036, 2, "Maximum charge power", "RO", "live"),
    (10038, 2, "Maximum discharge power", "RO", "live"),
    (10064, 1, "Work mode (0 self-consumption, 1 time-of-use, 3 third-party control, 4 custom, 5 socket overlay, 6 smart, 7 dynamic pricing)", "RW", "setting"),
    (10071, 2, "Battery charge/discharge setpoint (+ discharge, - charge; third-party mode only)", "RW", "setting"),
    (10100, 12, "Serial number", "RO", "info"),
    (10112, 6, "Software version", "RO", "info"),
    (10208, 2, "AC grid-side output power", "RO", "live"),
    (10250, 2, "Rated energy", "RO", "info"),
    (10262, 2, "Cumulative charge energy", "RO", "live"),
    (10264, 2, "Cumulative discharge energy", "RO", "live"),
    (32768, 5, "Model", "RO", "info"),
    (32774, 1, "Work-mode capability mask", "RO", "info"),
    (32775, 1, "Capability mask (bit0 charge limit, bit1 discharge limit, bit2 backup reserve, bit3 backup enable)", "RO", "info"),
    (60000, 1, "Charge limit SOC (%, 80-100)", "RW", "setting"),
    (60001, 1, "Discharge limit SOC (%, 0-20)", "RW", "setting"),
    (60002, 1, "Backup reserve SOC (%)", "RW", "setting"),
    (60003, 1, "Backup reserve enabled (0 off, 1 on)", "RO", "setting"),
)

# Alarm bits worth telling the user about (System Alarm 1 = register 10042, bit numbers as in the document).
X1_ALARMS: dict[int, str] = {
    0: "EEPROM read/write error",
    1: "flash read/write error",
    4: "inverter (DC/AC) communication failure",
    5: "battery (BMS) communication failure",
    6: "meter 1 communication failure",
    7: "meter 2 communication failure",
}


def describe_all(x1: bool = True) -> dict[str, list[str]]:
    """{register address: [name, access, kind]} for every register covered, for the panel."""
    out: dict[str, list[str]] = {}
    for addr, n, name, access, kind in (X1_REGISTERS if x1 else SOLARBANK_REGISTERS):
        for i in range(n):
            out[str(addr + i)] = [name, access, kind]
    return out


def alarm_texts(register_value: int) -> list[str]:
    return [text for bit, text in X1_ALARMS.items() if register_value & (1 << bit)]
