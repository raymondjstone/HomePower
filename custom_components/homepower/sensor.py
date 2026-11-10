"""HomePower sensors: whole-system totals plus per-battery target/decision."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HomePowerCoordinator


def _info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)}, name="HomePower", manufacturer="HomePower",
        configuration_url="homeassistant://homepower",  # "Visit" button on the device page
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: HomePowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    add([BatteryFlow(coord, entry), StoredEnergy(coord, entry), GridPower(coord, entry)])
    known: set[str] = set()

    def _sync() -> None:
        new = [a.config for a in coord.adapters if a.config.device_id not in known]
        known.update(c.device_id for c in new)
        ents = []
        for c in new:
            ents += [BatterySoc(coord, entry, c.device_id, c.name), BatteryDevicePower(coord, entry, c.device_id, c.name),
                     BatteryTarget(coord, entry, c.device_id, c.name)]
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
        vals = [s.actual.power_w for s in self.coordinator._snap.devices.values()
                if s.actual and s.actual.online and s.actual.power_w is not None]
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
            st = self.coordinator._snap.devices.get(a.config.device_id)
            if st and st.actual and st.actual.online and st.actual.soc is not None:
                total += st.actual.soc / 100 * a.config.caps.capacity_wh
                seen = True
        return round(total / 1000, 2) if seen else None


class BatteryTarget(_Base):
    """What HomePower currently wants this battery to do (state) and why (attributes)."""

    _attr_icon = "mdi:home-battery"

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
        return "paused" if self.coordinator.paused else (st.desired.mode.value if st.desired else "unmanaged")

    @property
    def extra_state_attributes(self):
        v = self.coordinator.view()["devices"].get(self._did, {})
        return {k: v.get(k) for k in ("decision", "desired_power_w", "mode", "soc", "power_w", "online",
                                     "active_schedules", "override", "last_error")}


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
        v = self.coordinator.view().get("grid_w")
        return None if v is None else round(v)


class _BatteryBase(_Base):
    def __init__(self, coord, entry, device_id: str, name: str, suffix: str, label: str) -> None:
        super().__init__(coord, entry)
        self._did = device_id
        self._attr_name = f"{name} {label}"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{suffix}"

    def _actual(self):
        st = self.coordinator._snap.devices.get(self._did)
        return st.actual if st and st.actual and st.actual.online else None


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
