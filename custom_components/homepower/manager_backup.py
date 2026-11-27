"""Backup and restore: the automatic daily copies, and exporting / importing everything to a file.

Part of HomePowerManager (manager.py), kept in its own file so each file has one concern: the methods run on
the manager itself and use its state."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import logging

from .core.insights import SceneError, clean_scene
from .core.serde import (
    ValidationError,
    device_from_dict,
    device_to_dict,
    goal_from_dict,
    goal_to_dict,
    schedule_from_dict,
    schedule_to_dict,
)

_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".manager")   # the manager's own logger


class BackupMixin:
    if TYPE_CHECKING:    # used here but defined on the class this is mixed into: declared for the type checker only
        devices: Any
        schedules: Any
        goals: Any
        scenes: Any
        _apply: Any
        _check_new_device: Any
        _hand_back: Any
        _with_saved_secret: Any
        hass: Any

    def _backup_dir(self) -> str:
        return self.hass.config.path("homepower_backups")

    async def list_backups(self) -> list[dict]:
        def run() -> list[dict]:
            from pathlib import Path

            from . import backup_files

            d = Path(self._backup_dir())
            return [{"name": p.name, "size": p.stat().st_size} for p in sorted(backup_files(d), reverse=True)] if d.is_dir() else []

        return await self.hass.async_add_executor_job(run)

    async def restore_backup(self, name: str, entry) -> dict:
        import json as _json
        import re as _re
        from pathlib import Path

        if not _re.fullmatch(r"homepower-\d{4}-\d\d-\d\d\.json", name or ""):
            raise ValidationError("not one of HomePower's backup files")

        def run() -> dict:
            return _json.loads((Path(self._backup_dir()) / name).read_text(encoding="utf8"))

        try:
            data = await self.hass.async_add_executor_job(run)
        except (OSError, ValueError) as err:
            raise ValidationError(f"could not read that backup: {err}") from err
        return await self.import_all(data, entry)

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
        """Restore from export_all(). Every row is validated on its own; bad ones are reported, never half-applied: if anything
        unexpected goes wrong the stored batteries, schedules, goals and scenes are put back as they were."""
        if not isinstance(data, dict) or data.get("homepower_export") != 1:
            raise ValidationError("this is not a HomePower export file")
        before = (dict(self.devices), dict(self.schedules), dict(self.goals), dict(self.scenes))
        options_before = dict(entry.options) if entry is not None else None
        try:
            return await self._import_rows(data, entry)
        except Exception as err:  # noqa: BLE001 - nothing may stay half restored
            self.devices, self.schedules, self.goals, self.scenes = before
            if entry is not None and dict(entry.options) != options_before:
                self.hass.config_entries.async_update_entry(entry, options=options_before)   # the settings too
            if isinstance(err, ValidationError):
                raise
            _LOGGER.warning("restore failed, nothing was changed", exc_info=True)
            raise ValidationError(f"the file could not be restored ({type(err).__name__}); nothing was changed") from err

    @staticmethod
    def _row_name(raw, *keys: str) -> str:
        return next((str(raw[k]) for k in keys if isinstance(raw, dict) and raw.get(k)), "?")

    async def _import_rows(self, data: dict, entry) -> dict:
        added, skipped = {"devices": 0, "schedules": 0, "goals": 0, "scenes": 0}, []
        for raw in data.get("devices") or []:
            try:
                if not isinstance(raw, dict):
                    raise ValidationError("not a battery entry")
                raw = self._with_saved_secret(dict(raw))
                self._check_new_device(raw)
                d = device_to_dict(device_from_dict(raw))
                old = self.devices.get(d["device_id"])
                if old and old.get("control_enabled", True) and not d["control_enabled"]:
                    await self._hand_back(d["device_id"])        # control is being revoked by the restore: do not leave it forced
                self.devices[d["device_id"]] = d
                added["devices"] += 1
            except (ValidationError, KeyError, TypeError, ValueError, AttributeError, OverflowError) as err:
                name = self._row_name(raw, "name", "device_id")
                skipped.append(f"battery {name}: {err} (re-add it, keys are not in the file)" if "required" in str(err) else f"battery {name}: {err}")
        for kind, coll, parse, dump in (("schedules", self.schedules, schedule_from_dict, schedule_to_dict),
                                        ("goals", self.goals, goal_from_dict, goal_to_dict)):
            for raw in data.get(kind) or []:
                try:
                    row = dump(parse(raw))
                    coll[row["id"]] = row
                    added[kind] += 1
                except (ValidationError, KeyError, TypeError, ValueError, AttributeError, OverflowError) as err:
                    skipped.append(f"{kind[:-1]} {self._row_name(raw, 'id')}: {err}")
        for raw in data.get("scenes") or []:
            try:
                s = clean_scene(raw)
                self.scenes[s["name"]] = s
                added["scenes"] += 1
            except (SceneError, KeyError, TypeError, ValueError, AttributeError, OverflowError) as err:
                skipped.append(f"scene {self._row_name(raw, 'name')}: {err}")
        if isinstance(data.get("settings"), dict) and entry is not None:
            from .core.settings import SettingsError, clean

            try:
                self.hass.config_entries.async_update_entry(entry, options={**entry.options, **clean(data["settings"])})
                added["settings"] = len(data["settings"])
            except SettingsError as err:
                skipped.append(f"settings: {err}")
        await self._apply()
        return {"added": added, "skipped": skipped}
