"""Persistent batteries, schedules and goals; runtime flags; and the bridge to the coordinator."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .adapters.direct import DirectAdapter
from .adapters.ha_entities import HaEntityAdapter
from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION
from .coordinator import HomePowerCoordinator
from .core.insights import DEFAULT_SCENES, SceneError, clean_scene
from .core.models import Desired, Mode
from .core.profiles import PROFILE_BY_ID
from .core.serde import (
    ValidationError,
    device_from_dict,
    device_to_dict,
    fill_from_probe,
    goal_from_dict,
    goal_to_dict,
    schedule_from_dict,
    schedule_to_dict,
)
from .drivers import discovery
from .drivers.anker import AnkerModbusDriver
from .drivers.ecoflow import EcoFlowApi, EcoFlowError, EcoFlowStreamDriver
from .drivers.ecoflow_live import EcoFlowLive
from .drivers.modbus_generic import GenericModbusDriver


_LOGGER = logging.getLogger(__name__)


class HomePowerManager:
    """Source of truth for user-defined batteries, schedules and goals (JSON dicts, validated)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.devices: dict[str, dict] = {}
        self.schedules: dict[str, dict] = {}
        self.goals: dict[str, dict] = {}
        self.coordinator: HomePowerCoordinator | None = None
        self.paused = False
        self.backup = False       # manual "backup mode" (keep a reserve)
        self.away = False         # manual "away / holiday" mode
        self.energy: dict[str, float] = {}       # lifetime kWh counters
        self.ledger_days: dict[str, dict] = {}   # savings ledger
        self.profile_data: dict = {}             # learned usual household load
        self.daily_data: dict = {}               # daily self-consumption / cheap-rate figures
        self.health_data: dict = {}              # weekly battery efficiency
        self.scenes: dict[str, dict] = {}        # saved combinations of modes and limits
        self.event_state: dict[str, bool] = {}   # heads-up messages currently showing
        self.tests: dict[str, dict] = {}         # last control-test result per battery
        self.pv_seen: set[str] = set()           # batteries that have reported solar power (so they have panels)
        self.divert_on: str = ""                 # the solar-surplus load we switched on (to switch it off after a restart)
        self._adapters: dict[str, tuple[dict, object]] = {}  # reuse live connections between rebuilds
        self.forced: set[str] = set()  # batteries HomePower has put into forced control
        self._live_on = False                       # opt-in setting: EcoFlow live (MQTT) updates
        self._hubs: dict[tuple, EcoFlowLive] = {}   # live-update connections, one per EcoFlow account

    # ------------------------------------------------------------------ persistence
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.devices = {d["device_id"]: d for d in data.get("devices", [])}
        self.schedules = {s["id"]: s for s in data.get("schedules", [])}
        self.goals = {g["id"]: g for g in data.get("goals", [])}
        self.paused = bool(data.get("paused", False))
        self.backup = bool(data.get("backup", False))
        self.away = bool(data.get("away", False))
        self.forced = set(data.get("forced", []))
        self.energy = dict(data.get("energy", {}))
        self.ledger_days = dict(data.get("ledger", {}))
        self.profile_data, self.daily_data = dict(data.get("profile", {})), dict(data.get("daily", {}))
        self.health_data = dict(data.get("health", {}))
        self.event_state, self.tests = dict(data.get("event_state", {})), dict(data.get("tests", {}))
        self.divert_on = str(data.get("divert_on") or "")
        self.pv_seen = set(data.get("pv_seen", []))
        scenes = data.get("scenes")
        self.scenes = {s["name"]: s for s in (scenes if scenes is not None else DEFAULT_SCENES)}

    async def _save(self) -> None:
        c = self.coordinator
        await self._store.async_save({
            "devices": list(self.devices.values()), "schedules": list(self.schedules.values()),
            "goals": list(self.goals.values()), "paused": self.paused, "backup": self.backup, "away": self.away,
            "forced": sorted(self.forced),
            "energy": c.meter.totals if c else self.energy, "ledger": c.ledger.dump() if c else self.ledger_days,
            "profile": c.profile.dump() if c else self.profile_data, "daily": c.daily.dump() if c else self.daily_data,
            "health": c.health.dump() if c else self.health_data, "scenes": list(self.scenes.values()),
            "event_state": {k: v for k, v in (c._event_state if c else self.event_state).items() if v}, "tests": self.tests, "divert_on": self.divert_on, "pv_seen": sorted(self.pv_seen),
        })

    async def save_runtime(self) -> None:
        await self._save()

    def on_forced_change(self, ids: set[str]) -> None:
        self.forced = ids
        self.hass.async_create_task(self._save())

    # ------------------------------------------------------------------ building the live objects
    def make_adapter(self, cfg):
        driver = cfg.extra.get("driver", "entities")
        if driver == "anker_modbus":
            return DirectAdapter(cfg, AnkerModbusDriver(cfg))
        if driver == "ecoflow_stream":
            return DirectAdapter(cfg, EcoFlowStreamDriver(cfg, async_get_clientsession(self.hass), self._live_hub(cfg)))
        if driver == "modbus_generic":
            return DirectAdapter(cfg, GenericModbusDriver(cfg))
        return HaEntityAdapter(self.hass, cfg)

    def _live_hub(self, cfg) -> EcoFlowLive | None:
        """One shared live-update connection per EcoFlow account."""
        c = cfg.extra.get("connection", {})
        key = (c.get("access_key"), c.get("region"))
        hub = self._hubs.get(key)
        if hub is None:
            try:
                hub = self._hubs[key] = EcoFlowLive(EcoFlowApi(async_get_clientsession(self.hass), c.get("region", "global"),
                                                              c["access_key"], c["secret_key"]), self.hass.loop)
            except Exception:  # noqa: BLE001 - live updates are optional
                return None
            hub.enabled = self._live_on
        return hub

    def set_live(self, on: bool) -> None:
        self._live_on = on
        for hub in self._hubs.values():
            hub.enabled = on
            if not on:
                hub.stop()

    def build(self):
        """Validate everything stored and build adapters/schedules/goals; bad rows are skipped.

        Unchanged batteries keep their adapter (and open connection / remembered state)."""
        adapters, schedules, goals, keep = [], [], [], {}
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
        for g in self.goals.values():
            try:
                goals.append(goal_from_dict(g))
            except ValidationError:
                continue
        return adapters, schedules, goals

    async def async_close(self) -> None:
        for hub in self._hubs.values():
            hub.stop()
        for _, adapter in self._adapters.values():
            await adapter.close()

    async def _apply(self) -> None:
        await self._save()
        if self.coordinator:
            self.coordinator.set_config(*self.build())
            await self.coordinator.async_request_refresh()

    # ------------------------------------------------------------------ setup helpers (panel + Configure flow)
    async def probe(self, raw: dict) -> dict:
        """Connect with the given settings and return live readings; never changes anything on the device."""
        raw = self._with_saved_secret(raw)
        # Placeholder limits only so the config validates; probing never uses or saves them.
        probe_raw = {"capacity_wh": 1000, "max_charge_w": 1000, "max_discharge_w": 1000,
                     **{k: v for k, v in raw.items() if v not in (None, "")},
                     "device_id": raw.get("device_id") or "probe", "name": raw.get("name") or "probe"}
        try:
            cfg = device_from_dict(probe_raw)
        except ValidationError as err:
            raise ValidationError(str(err)) from err
        live = self._live_adapter_for(raw, cfg)
        adapter = live or self.make_adapter(cfg)
        try:
            if not hasattr(adapter, "driver"):
                return {}
            return await adapter.driver.probe()
        except Exception as err:  # noqa: BLE001 - shown to the user as "could not connect"
            raise ValidationError(f"could not read the battery: {err}") from err
        finally:
            if live is None:   # never close the connection the control loop is using
                await adapter.close()

    def _live_adapter_for(self, raw: dict, cfg):
        """Testing a battery that is already connected reuses its connection: devices that accept a single
        client would otherwise refuse (or disturb) the second one."""
        cached = self._adapters.get(raw.get("device_id") or "")
        if cached is None:
            return None
        old_conn = cached[0].get("connection", {})
        new_conn = cfg.extra.get("connection", {})
        keys = ("host", "port", "unit_id", "sn", "region", "access_key", "secret_key")
        return cached[1] if all(old_conn.get(k) == new_conn.get(k) for k in keys) else None

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

    # ------------------------------------------------------------------ flags
    async def set_paused(self, paused: bool) -> None:
        self.paused = paused
        await self._save()
        if self.coordinator:
            self.coordinator.async_update_listeners()

    async def set_backup(self, on: bool) -> None:
        self.backup = on
        await self._save()
        if self.coordinator:
            await self.coordinator.async_request_refresh()

    async def set_away(self, on: bool) -> None:
        self.away = on
        await self._save()
        if self.coordinator:
            await self.coordinator.async_request_refresh()

    # ------------------------------------------------------------------ mutations (raise ValidationError on bad input)
    async def _autofill(self, raw: dict) -> dict:
        """Capacity, power limits and minimum SOC come from the battery itself, not from the user."""
        profile = PROFILE_BY_ID.get(raw.get("kind"), PROFILE_BY_ID["custom"])
        driver = profile["driver"]
        if driver == "entities":
            return raw
        need = ("capacity_wh", "max_charge_w", "max_discharge_w", "min_soc")
        if all(raw.get(k) not in (None, "") for k in need):
            return raw
        try:
            raw = fill_from_probe(raw, await self.probe(raw))
        except ValidationError:
            pass  # offline right now: fall through to the normal validation message
        if driver == "ecoflow_stream" or (driver == "modbus_generic" and not self._map_controls(raw)):
            raw.setdefault("max_charge_w", 0)   # nothing to size: no charge/discharge setpoint exists
            raw.setdefault("max_discharge_w", 0)
        raw.setdefault("min_soc", 10)  # safe floor if the battery doesn't report one
        return raw

    @staticmethod
    def _map_controls(raw: dict) -> bool:
        from .core.modbus_map import MapError, has_control, parse_map

        conn = raw.get("connection") or {}
        mp = conn.get("map") or PROFILE_BY_ID.get(raw.get("kind"), {}).get("preset_map")
        try:
            return has_control(parse_map(mp))
        except MapError:
            return False

    async def _hand_back(self, device_id: str) -> None:
        """Best effort: give a battery we are forcing back to its own control before we stop managing it."""
        cached = self._adapters.get(device_id)
        if cached is None or device_id not in self.forced:
            return
        try:
            await cached[1].apply(Desired(Mode.AUTO))
            self.forced.discard(device_id)
            if self.coordinator and device_id in self.coordinator._snap.devices:
                self.coordinator._snap.devices[device_id].release_pending = False
        except Exception:  # noqa: BLE001 - the flag stays set; the control loop retries while it still can
            _LOGGER.warning("could not hand %s back before stopping control", device_id, exc_info=True)

    async def save_device(self, raw: dict) -> dict:
        d = device_to_dict(device_from_dict(await self._autofill(self._with_saved_secret(raw))))
        old = self.devices.get(d["device_id"])
        if old and old.get("control_enabled", True) and not d["control_enabled"]:
            await self._hand_back(d["device_id"])   # control is being revoked: don't leave it forced
        self.devices[d["device_id"]] = d
        await self._apply()
        return d

    async def delete_device(self, device_id: str) -> None:
        await self._hand_back(device_id)
        self.devices.pop(device_id, None)
        self.forced.discard(device_id)
        self.tests.pop(device_id, None)
        self.pv_seen.discard(device_id)
        self._remove_entities(device_id)
        for coll in (self.schedules, self.goals):  # drop references; an empty list would mean "all batteries"
            for item in coll.values():
                if device_id in item.get("device_ids", []):
                    item["device_ids"] = [x for x in item["device_ids"] if x != device_id]
                    if not item["device_ids"]:
                        item["enabled"] = False
        await self._apply()

    def _remove_entities(self, device_id: str) -> None:
        """Drop the sensors of a deleted battery so they don't linger as 'unavailable'."""
        from homeassistant.helpers import entity_registry as er

        c = self.coordinator
        if c is None:
            return
        try:  # cosmetic clean-up: it must never stop a battery being deleted
            reg, eid = er.async_get(self.hass), c.config_entry.entry_id
            prefixes = (f"{eid}_{device_id}_", f"{eid}_energy_bat_{device_id}_")
            for ent in list(er.async_entries_for_config_entry(reg, eid)):
                if ent.unique_id.startswith(prefixes):
                    reg.async_remove(ent.entity_id)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("could not remove entities for %s", device_id, exc_info=True)

    async def save_schedule(self, raw: dict) -> dict:
        s = schedule_to_dict(schedule_from_dict(raw))
        self.schedules[s["id"]] = s
        await self._apply()
        return s

    async def delete_schedule(self, schedule_id: str) -> None:
        self.schedules.pop(schedule_id, None)
        await self._apply()

    async def save_goal(self, raw: dict) -> dict:
        g = goal_to_dict(goal_from_dict(raw))
        self.goals[g["id"]] = g
        await self._apply()
        return g

    async def delete_goal(self, goal_id: str) -> None:
        self.goals.pop(goal_id, None)
        await self._apply()

    # ------------------------------------------------------------------ scenes
    async def save_scene(self, raw: dict) -> dict:
        try:
            s = clean_scene(raw)
        except SceneError as err:
            raise ValidationError(str(err)) from err
        self.scenes[s["name"]] = s
        await self._save()
        return s

    async def delete_scene(self, name: str) -> None:
        self.scenes.pop(name, None)
        await self._save()

    async def apply_scene(self, name: str) -> None:
        """Switch to a saved scene: its flags, and any limits it sets (others stay as they are)."""
        s = self.scenes.get(name)
        if s is None:
            raise ValidationError(f"no scene called {name}")
        self.backup, self.away, self.paused = bool(s["backup"]), bool(s["away"]), bool(s["paused"])
        c = self.coordinator
        if s["settings"] and c is not None:
            entry = c.config_entry
            self.hass.config_entries.async_update_entry(entry, options={**entry.options, **s["settings"]})
        await self._save()
        if c is not None:
            c.async_update_listeners()
            await c.async_request_refresh()

    # ------------------------------------------------------------------ backup / restore
    def export_all(self, settings: dict, include_secrets: bool = False) -> dict:
        devices = []
        for d in self.devices.values():
            d = {**d, "connection": dict(d.get("connection", {}))}
            if not include_secrets:
                for k in ("secret_key", "access_key"):
                    if k in d["connection"]:
                        d["connection"][k] = ""
            devices.append(d)
        return {"homepower_export": 1, "includes_secrets": include_secrets, "devices": devices,
                "schedules": list(self.schedules.values()), "goals": list(self.goals.values()),
                "scenes": list(self.scenes.values()), "settings": settings}

    async def import_all(self, data: dict, entry) -> dict:
        """Restore from export_all(). Every row is validated on its own; bad ones are reported, never half-applied."""
        if not isinstance(data, dict) or data.get("homepower_export") != 1:
            raise ValidationError("this is not a HomePower export file")
        added, skipped = {"devices": 0, "schedules": 0, "goals": 0, "scenes": 0}, []
        for raw in data.get("devices") or []:
            try:
                raw = self._with_saved_secret(dict(raw))
                d = device_to_dict(device_from_dict(raw))
                self.devices[d["device_id"]] = d
                added["devices"] += 1
            except (ValidationError, KeyError, TypeError) as err:
                skipped.append(f"battery {raw.get('name') or raw.get('device_id')}: {err} (re-add it, keys are not in the file)"
                               if "required" in str(err) else f"battery {raw.get('name') or raw.get('device_id')}: {err}")
        for kind, coll, parse, dump in (("schedules", self.schedules, schedule_from_dict, schedule_to_dict),
                                        ("goals", self.goals, goal_from_dict, goal_to_dict)):
            for raw in data.get(kind) or []:
                try:
                    row = dump(parse(raw))
                    coll[row["id"]] = row
                    added[kind] += 1
                except (ValidationError, KeyError, TypeError) as err:
                    skipped.append(f"{kind[:-1]} {raw.get('id')}: {err}")
        for raw in data.get("scenes") or []:
            try:
                s = clean_scene(raw)
                self.scenes[s["name"]] = s
                added["scenes"] += 1
            except SceneError as err:
                skipped.append(f"scene {raw.get('name')}: {err}")
        if isinstance(data.get("settings"), dict) and entry is not None:
            from .core.settings import SettingsError, clean

            try:
                self.hass.config_entries.async_update_entry(entry, options={**entry.options, **clean(data["settings"])})
                added["settings"] = len(data["settings"])
            except SettingsError as err:
                skipped.append(f"settings: {err}")
        await self._apply()
        return {"added": added, "skipped": skipped}

    # ------------------------------------------------------------------ views
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

    def entity_ids(self) -> dict:
        """HA entity ids of HomePower's own sensors (the panel uses them for long-term history)."""
        from homeassistant.helpers import entity_registry as er

        c = self.coordinator
        if c is None:
            return {}
        try:  # only used for the history charts: never let it break the panel's main payload
            reg = er.async_get(self.hass)
            eid = c.config_entry.entry_id

            def find(uid: str) -> str | None:
                return reg.async_get_entity_id("sensor", DOMAIN, f"{eid}_{uid}")

            return {"battery_power": find("battery_power"), "grid_power": find("grid_power"),
                    "devices": {d: {"soc": find(f"{d}_soc"), "power": find(f"{d}_power")} for d in self.devices}}
        except Exception:  # noqa: BLE001
            _LOGGER.debug("entity id lookup failed", exc_info=True)
            return {}
