"""HomePower sensors: whole-system totals plus per-battery target/decision."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, PERCENTAGE, UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coord_base import MONEY_EXPORT, MONEY_SAVED
from .coordinator import HomePowerCoordinator


def _info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)}, name="HomePower", manufacturer="HomePower", entry_type=DeviceEntryType.SERVICE,
        configuration_url="homeassistant://homepower",  # "Visit" button on the device page
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: HomePowerCoordinator = entry.runtime_data.coordinator
    add([BatteryFlow(coord, entry), StoredEnergy(coord, entry), GridPower(coord, entry),
         EnergyTotal(coord, entry, "grid_in", "Grid energy imported"), EnergyTotal(coord, entry, "grid_out", "Grid energy exported"),
         EnergyTotal(coord, entry, "pv_in", "Solar energy"), Savings(coord, entry, "today"), Savings(coord, entry, "month"), PriceNow(coord, entry),
         MoneyTotal(coord, entry, MONEY_EXPORT, "Export earnings", "mdi:cash-plus"), MoneyTotal(coord, entry, MONEY_SAVED, "Battery savings", "mdi:piggy-bank-outline"),
         HomeBattery(coord, entry), DailyShare(coord, entry, "self_consumption", "Solar self-consumption today"),
         DailyShare(coord, entry, "cheap_share", "Cheap-rate share of grid import today")])
    known: set[str] = set()
    names: dict[str, str] = {}              # each battery's name when its sensors were made, to follow a rename
    made: dict[str, list] = {}

    def _rename(did: str, old: str, new: str) -> None:
        """Renamed in the panel: its sensors' names follow straight away (their entity ids stay, so automations keep working)."""
        for e in made.get(did, []):
            if isinstance(e._attr_name, str) and e._attr_name.startswith(old):
                e._attr_name = new + e._attr_name[len(old):]
                if e.hass is not None:
                    e.async_write_ha_state()

    def _sync() -> None:
        known.intersection_update({a.config.device_id for a in coord.adapters})   # a re-added battery gets fresh sensors
        for a in coord.adapters:
            did, n = a.config.device_id, a.config.name
            if did in known and names.get(did) not in (None, n):
                _rename(did, names[did], n)
            names[did] = n
        new = [a.config for a in coord.adapters if a.config.device_id not in known]
        known.update(c.device_id for c in new)
        ents = []
        for c in new:
            first = len(ents)
            ents += [BatterySoc(coord, entry, c.device_id, c.name), BatteryDevicePower(coord, entry, c.device_id, c.name),
                     BatterySolarPower(coord, entry, c.device_id, c.name),
                     BatteryTarget(coord, entry, c.device_id, c.name),
                     EnergyTotal(coord, entry, f"bat:{c.device_id}_in", f"{c.name} energy charged"),
                     EnergyTotal(coord, entry, f"bat:{c.device_id}_out", f"{c.name} energy discharged"),
                     Efficiency(coord, entry, c.device_id, c.name), Cycles(coord, entry, c.device_id, c.name, c.caps.capacity_wh)]
            if c.extra.get("house_meter"):     # Anker X1: the inverter's own PV inputs
                ents.append(PvTotal(coord, entry, c.device_id, c.name))
                for i in range(8):
                    ents += [PvString(coord, entry, c.device_id, c.name, i, kind) for kind in ("voltage", "current", "power")]
            made[c.device_id], names[c.device_id] = ents[first:], c.name
        add(ents)

    _sync()
    entry.async_on_unload(coord.async_add_listener(_sync))


class _Base(CoordinatorEntity[HomePowerCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord: HomePowerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coord)
        self._entry = entry
        self._attr_device_info = _info(entry)


class BatteryFlow(_Base):
    """Net power across all batteries: + charging, - discharging."""

    _attr_name = "Battery power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_battery_power"

    @property
    def native_value(self):
        vals = [a.power_w for a in self.coordinator.shown_actuals().values() if a.online and a.power_w is not None]
        return round(sum(vals)) if vals else None


class StoredEnergy(_Base):
    _attr_name = "Stored energy"
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_stored_energy"

    @property
    def native_value(self):
        total, seen = 0.0, False
        for a in self.coordinator.adapters:
            act = self.coordinator.shown_actual(a.config.device_id)
            if act and act.online and act.soc is not None:
                total += act.soc / 100 * a.config.caps.capacity_wh
                seen = True
        return round(total / 1000, 2) if seen else None


class BatteryTarget(_Base):
    """What HomePower currently wants this battery to do (state) and why (attributes)."""

    _attr_icon = "mdi:home-battery"
    _unrecorded_attributes = frozenset({MATCH_ALL})   # they change every cycle and have sensors of their own: keep the database small

    def __init__(self, coord, entry, device_id: str, name: str) -> None:
        super().__init__(coord, entry)
        self._did = device_id
        self._attr_name = f"{name} target"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_target"

    @property
    def native_value(self):
        st = self.coordinator._snap.devices.get(self._did)
        if not st:
            return None
        if self.coordinator.paused:
            return "paused"
        a = next((x for x in self.coordinator.adapters if x.config.device_id == self._did), None)
        if a is not None and not a.config.extra.get("control_enabled", True):
            return "monitor_only"           # what the plan would want is never sent to it: don't show it as if it were
        return st.desired.mode.value if st.desired else "unmanaged"

    @property
    def extra_state_attributes(self):
        v = self.coordinator.devices_view().get(self._did, {})
        return {k: v.get(k) for k in ("decision", "desired_power_w", "mode", "soc", "power_w", "online", "stale",
                                     "active_schedules", "override", "last_error", "reason")}


class GridPower(_Base):
    """House grid power as seen by the batteries / your grid sensor: + importing, - exporting."""

    _attr_name = "Grid power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_grid_power"

    @property
    def native_value(self):
        v = self.coordinator.last_grid
        return None if v is None else round(v)


class _BatteryBase(_Base):
    def __init__(self, coord, entry, device_id: str, name: str, suffix: str, label: str) -> None:
        super().__init__(coord, entry)
        self._did = device_id
        self._attr_name = f"{name} {label}"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{suffix}"

    def _actual(self):
        return self.coordinator.shown_actual(self._did)     # the last reading while the readings are only stale


class BatterySoc(_BatteryBase):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coord, entry, device_id: str, name: str) -> None:
        super().__init__(coord, entry, device_id, name, "soc", "charge")

    @property
    def native_value(self):
        a = self._actual()
        return None if a is None or a.soc is None else round(a.soc, 1)


class BatteryDevicePower(_BatteryBase):
    """+ charging, - discharging."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coord, entry, device_id: str, name: str) -> None:
        super().__init__(coord, entry, device_id, name, "power", "power")

    @property
    def native_value(self):
        a = self._actual()
        return None if a is None or a.power_w is None else round(a.power_w)


class BatterySolarPower(_BatteryBase):
    """The solar power the battery itself reports: the "Solar" figure on the side panel's battery cards
    (Anker: register 10002, PV power; EcoFlow: its PV sum). On an X1 "PV input power" below is the
    inverter's separate total (10183), so the two can be compared."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:solar-power"

    def __init__(self, coord, entry, device_id: str, name: str) -> None:
        super().__init__(coord, entry, device_id, name, "solar", "solar power")

    @property
    def native_value(self):
        a = self._actual()
        return None if a is None or a.pv_w is None else round(a.pv_w)


class PvTotal(_BatteryBase):
    """X1: total solar power at the inverter's PV inputs (register 10183)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coord, entry, device_id: str, name: str) -> None:
        super().__init__(coord, entry, device_id, name, "pv_total", "PV input power")

    @property
    def native_value(self):
        a = self._actual()
        return None if a is None or a.pv_total_w is None else round(a.pv_total_w)


class PvString(_BatteryBase):
    """X1: one PV string's voltage, current or power (V x A). Strings 5-8 are off until you enable them."""

    _KINDS = {"voltage": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, 1),
              "current": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE, 2),
              "power": (SensorDeviceClass.POWER, UnitOfPower.WATT, 0)}
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord, entry, device_id: str, name: str, index: int, kind: str) -> None:
        super().__init__(coord, entry, device_id, name, f"pv{index + 1}_{kind}", f"PV{index + 1} {kind}")
        self._i, self._kind = index, kind
        self._attr_device_class, self._attr_native_unit_of_measurement, self._digits = self._KINDS[kind]
        self._attr_entity_registry_enabled_default = index < 4

    @property
    def native_value(self):
        a = self._actual()
        if a is None or len(a.pv_strings) <= self._i:
            return None
        v, amps = a.pv_strings[self._i]
        return round({"voltage": v, "current": amps, "power": v * amps}[self._kind], self._digits)


class EnergyTotal(_Base):
    """Lifetime kWh. Add these to the HA Energy dashboard (battery charged / discharged, grid, solar)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coord, entry, key: str, name: str) -> None:
        super().__init__(coord, entry)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_energy_{key.replace(':', '_')}"

    @property
    def native_value(self):
        return round(self.coordinator.meter.get(self._key), 3)


class Savings(_Base):
    """Estimated money saved (avoided import minus the cost of grid charging). See the README for how."""

    _attr_icon = "mdi:piggy-bank"
    _attr_state_class = None

    def __init__(self, coord, entry, period: str) -> None:
        super().__init__(coord, entry)
        self._period = period
        self._attr_name = f"Savings {'today' if period == 'today' else 'this month'}"
        self._attr_unique_id = f"{entry.entry_id}_savings_{period}"

    @property
    def native_unit_of_measurement(self):
        return self.coordinator.settings.currency

    @property
    def native_value(self):
        from homeassistant.util import dt as dt_util

        now = dt_util.now()
        key = now.strftime("%Y-%m-%d") if self._period == "today" else now.strftime("%Y-%m")
        return round(self.coordinator.ledger.saved(key), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


class MoneyTotal(_Base):
    """A lifetime money total Home Assistant keeps long-term statistics of: export earnings (every kWh sent to the grid at the
    export price then: usable as the Energy dashboard's export compensation) or the batteries' estimated savings. In Home
    Assistant's own currency (Settings > System > General), so the Energy dashboard accepts it."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(self, coord, entry, key: str, name: str, icon: str) -> None:
        super().__init__(coord, entry)
        self._key = key
        self._attr_name, self._attr_icon = name, icon
        self._attr_unique_id = f"{entry.entry_id}_{key.replace(':', '_')}_total"

    @property
    def native_unit_of_measurement(self):
        return getattr(self.coordinator.hass.config, "currency", None) or "GBP"

    @property
    def native_value(self):
        return round(self.coordinator.meter.totals.get(self._key, 0.0), 4) + 0.0


class PriceNow(_Base):
    _attr_name = "Electricity price now"
    _attr_icon = "mdi:cash-clock"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_price_now"

    @property
    def native_unit_of_measurement(self):
        return f"{self.coordinator.settings.currency}/kWh"

    @property
    def native_value(self):
        from homeassistant.util import dt as dt_util

        p = self.coordinator._price_now(self.coordinator._rates(), dt_util.now())
        return None if p is None else round(p, 4)


class Efficiency(_BatteryBase):
    """Round-trip efficiency: energy discharged / energy charged over the battery's recorded life."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:percent"

    def __init__(self, coord, entry, device_id: str, name: str) -> None:
        super().__init__(coord, entry, device_id, name, "efficiency", "round-trip efficiency")

    @property
    def native_value(self):
        m = self.coordinator.meter
        cin = m.get(f"bat:{self._did}_in")
        return round(100 * m.get(f"bat:{self._did}_out") / cin, 1) if cin >= 1.0 else None   # needs >= 1 kWh of history


class Cycles(_BatteryBase):
    # TOTAL, not TOTAL_INCREASING: the count follows edits to the capacity, so it can step down, and Home Assistant would read
    # a step down as a meter reset and add the whole count to its statistics again.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:battery-sync"
    _attr_native_unit_of_measurement = "cycles"

    def __init__(self, coord, entry, device_id: str, name: str, capacity_wh: int) -> None:
        super().__init__(coord, entry, device_id, name, "cycles", "cycles")

    @property
    def native_value(self):
        a = next((x for x in self.coordinator.adapters if x.config.device_id == self._did), None)
        cap_kwh = max(a.config.caps.capacity_wh if a else 1, 1) / 1000   # follows edits to the capacity
        return round(self.coordinator.meter.get(f"bat:{self._did}_out") / cap_kwh, 1)


class HomeBattery(_Base):
    """All batteries as one: charge level (state) plus totals in the attributes (the Lovelace card reads these)."""

    _attr_name = "Home battery"
    _unrecorded_attributes = frozenset({MATCH_ALL})   # live figures for the card; the charge level itself is recorded
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_home_battery"

    def _fig(self) -> dict:
        return self.coordinator._combined(self.coordinator.shown_actuals())

    @property
    def native_value(self):
        return self._fig()["soc"]

    @property
    def extra_state_attributes(self):
        fig = self._fig()
        v = self.coordinator.devices_view()
        return {**fig, "homepower": True,
                "batteries": [{"name": a.config.name, "soc": (v.get(a.config.device_id) or {}).get("soc"),
                               "power_w": (v.get(a.config.device_id) or {}).get("power_w"),
                               "online": bool((v.get(a.config.device_id) or {}).get("online")),
                               "stale": bool((v.get(a.config.device_id) or {}).get("stale"))} for a in self.coordinator.adapters]}


class DailyShare(_Base):
    """Today's solar self-consumption or cheap-rate share of imports, in percent."""

    _attr_device_class = None
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:percent"

    def __init__(self, coord, entry, key: str, name: str) -> None:
        super().__init__(coord, entry)
        self._key, self._attr_name = key, name
        self._attr_unique_id = f"{entry.entry_id}_{key}_today"

    @property
    def native_value(self):
        from homeassistant.util import dt as dt_util

        day = dt_util.now().strftime("%Y-%m-%d")
        return getattr(self.coordinator.daily, self._key)(day)
