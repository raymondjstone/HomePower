"""Persistent batteries + schedules, and the bridge between them and the coordinator."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .adapters.direct import DirectAdapter
from .adapters.ha_entities import HaEntityAdapter
from .const import STORAGE_KEY, STORAGE_VERSION
from .coordinator import HomePowerCoordinator
from .drivers import discovery
from .drivers.anker import AnkerModbusDriver
from .drivers.ecoflow import EcoFlowApi, EcoFlowError, EcoFlowStreamDriver
from .core.serde import (
    ValidationError,
    device_from_dict,
    device_to_dict,
    schedule_from_dict,
    schedule_to_dict,
)


class HomePowerManager:
    """Source of truth for user-defined batteries and schedules (JSON dicts, validated)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.devices: dict[str, dict] = {}
        self.schedules: dict[str, dict] = {}
        self.coordinator: HomePowerCoordinator | None = None
        self.paused = False
        self._adapters: dict[str, tuple[dict, object]] = {}  # reuse live connections between rebuilds
        self.forced: set[str] = set()  # batteries HomePower has put into forced control

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.devices = {d["device_id"]: d for d in data.get("devices", [])}
        self.schedules = {s["id"]: s for s in data.get("schedules", [])}
        self.paused = bool(data.get("paused", False))
        self.forced = set(data.get("forced", []))

    async def _save(self) -> None:
        await self._store.async_save(
            {"devices": list(self.devices.values()), "schedules": list(self.schedules.values()), "paused": self.paused, "forced": sorted(self.forced)}
        )

    def make_adapter(self, cfg):
        driver = cfg.extra.get("driver", "entities")
        if driver == "anker_modbus":
            return DirectAdapter(cfg, AnkerModbusDriver(cfg))
        if driver == "ecoflow_stream":
            return DirectAdapter(cfg, EcoFlowStreamDriver(cfg, async_get_clientsession(self.hass)))
        return HaEntityAdapter(self.hass, cfg)

    def build(self):
        """Validate everything stored and build adapters/schedules; bad rows are skipped.

        Unchanged batteries keep their adapter (and open connection / remembered state)."""
        adapters, schedules, keep = [], [], {}
        for d in self.devices.values():
            try:
                cached = self._adapters.get(d["device_id"])
                if cached and cached[0] == d:
                    adapter = cached[1]
                else:
                    adapter = self.make_adapter(device_from_dict(d))
                keep[d["device_id"]] = (d, adapter)
                adapters.append(adapter)
            except (ValidationError, KeyError):
                continue
        for did, (_, old) in self._adapters.items():
            if keep.get(did, (None, None))[1] is not old:
                self.hass.async_create_task(old.close())
        self._adapters = keep
        for s in self.schedules.values():
            try:
                schedules.append(schedule_from_dict(s))
            except ValidationError:
                continue
        return adapters, schedules

    async def async_close(self) -> None:
        for _, adapter in self._adapters.values():
            await adapter.close()

    # --- setup helpers used by the panel and the Configure flow ---
    async def probe(self, raw: dict) -> dict:
        """Connect with the given settings and return live readings; never changes anything on the device."""
        raw = self._with_saved_secret(raw)
        probe_raw = {"capacity_wh": 5000, "max_charge_w": 1000, "max_discharge_w": 1000, **{k: v for k, v in raw.items() if v not in (None, "")},
                     "device_id": raw.get("device_id") or "probe", "name": raw.get("name") or "probe"}
        cfg = device_from_dict(probe_raw)
        adapter = self.make_adapter(cfg)
        try:
            if not hasattr(adapter, "driver"):
                return {}
            return await adapter.driver.probe()
        except Exception as err:  # noqa: BLE001 - shown to the user as "could not connect"
            raise ValidationError(f"could not read the battery: {err}") from err
        finally:
            await adapter.close()

    async def discover_anker(self) -> list[dict]:
        hosts: list[str] = []
        try:
            from homeassistant.components import network

            for ad in await network.async_get_adapters(self.hass):
                if not ad.get("enabled"):
                    continue
                for v4 in ad.get("ipv4", []):
                    hosts += [h for h in discovery.subnet_hosts(v4["address"], int(v4["network_prefix"])) if h not in hosts]
        except Exception:  # noqa: BLE001
            return []
        return await discovery.scan(hosts)

    async def discover_ecoflow(self, region: str, access_key: str, secret_key: str) -> list[dict]:
        try:
            items = await EcoFlowApi(async_get_clientsession(self.hass), region, access_key, secret_key).devices()
        except EcoFlowError as err:
            raise ValidationError(str(err)) from err
        return [{"sn": i.get("sn"), "name": i.get("deviceName") or i.get("productName") or i.get("sn"),
                 "online": bool(i.get("online")), "product": i.get("productName")} for i in items if i.get("sn")]

    def _with_saved_secret(self, raw: dict) -> dict:
        """When editing, a blank secret key means 'keep the one already stored'."""
        conn = dict(raw.get("connection") or {})
        old = self.devices.get(raw.get("device_id") or "", {}).get("connection", {})
        if not conn.get("secret_key") and old.get("secret_key"):
            conn["secret_key"] = old["secret_key"]
        return {**raw, "connection": conn}

    async def _apply(self) -> None:
        await self._save()
        if self.coordinator:
            self.coordinator.set_config(*self.build())
            await self.coordinator.async_request_refresh()

    def on_forced_change(self, ids: set[str]) -> None:
        self.forced = ids
        self.hass.async_create_task(self._save())

    async def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if self.coordinator:
            self.coordinator.paused = paused
            self.coordinator.async_update_listeners()
        await self._save()

    # --- mutations (raise ValidationError on bad input) ---
    async def save_device(self, raw: dict) -> dict:
        d = device_to_dict(device_from_dict(self._with_saved_secret(raw)))
        self.devices[d["device_id"]] = d
        await self._apply()
        return d

    async def delete_device(self, device_id: str) -> None:
        self.devices.pop(device_id, None)
        for s in self.schedules.values():  # drop references; empty list would mean "all devices"
            if device_id in s.get("device_ids", []):
                s["device_ids"] = [x for x in s["device_ids"] if x != device_id]
                if not s["device_ids"]:
                    s["enabled"] = False
        await self._apply()

    async def save_schedule(self, raw: dict) -> dict:
        s = schedule_to_dict(schedule_from_dict(raw))
        self.schedules[s["id"]] = s
        await self._apply()
        return s

    async def delete_schedule(self, schedule_id: str) -> None:
        self.schedules.pop(schedule_id, None)
        await self._apply()

    def public_devices(self) -> list[dict]:
        """Devices for the UI: secrets are never sent to the browser."""
        out = []
        for d in self.devices.values():
            d = dict(d)
            conn = dict(d.get("connection", {}))
            if "secret_key" in conn:
                conn["secret_key"] = ""
                conn["secret_saved"] = True
            d["connection"] = conn
            out.append(d)
        return out
