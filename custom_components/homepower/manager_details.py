"""One battery up close: its registers (scan, read, the user's own names), the settings it lets you change, and everything it reports for the details sheet.

Part of HomePowerManager (manager.py), kept in its own file so each file has one concern: the methods run on
the manager itself and use its state."""
from __future__ import annotations

import logging

from .core.serde import ValidationError

_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".manager")   # the manager's own logger


class DetailsMixin:
    async def scan_registers(self, device_id: str) -> dict:
        """Raw register dump of a connected battery (read-only), for the panel's register compare tool."""
        cached = self._adapters.get(device_id)
        driver = getattr(cached[1], "driver", None) if cached else None
        if driver is None or not hasattr(driver, "scan"):
            raise ValidationError("this battery does not support a register scan")
        try:
            out = await driver.scan()
        except Exception as err:  # noqa: BLE001 - shown to the user
            raise ValidationError(f"could not scan: {err}") from err
        from .core.anker_x1_map import describe_all
        out["order"] = driver._order      # 32-bit word order, so the panel can decode two-register values
        out["text_low_first"] = bool(driver.config.extra.get("house_meter"))   # the X1 holds each text character pair low byte first; Solarbanks high byte first
        out["names"] = describe_all(bool(driver.config.extra.get("house_meter")))   # Anker's register lists: names, read/write, live-or-setting
        return out

    async def read_registers(self, device_id: str, addrs: list[int], names: bool = False) -> dict:
        """Current value of a few registers (read-only), for the panel's register watch."""
        cached = self._adapters.get(device_id)
        driver = getattr(cached[1], "driver", None) if cached else None
        if driver is None or not hasattr(driver, "read_registers"):
            raise ValidationError("this battery does not support reading registers")
        try:
            out = {"values": await driver.read_registers(addrs), "order": driver._order}
            if names:
                from .core.anker_x1_map import describe_all

                out["names"] = describe_all(bool(driver.config.extra.get("house_meter")))
            return out
        except Exception as err:  # noqa: BLE001 - shown to the user
            raise ValidationError(f"could not read: {err}") from err

    def _controlled(self, driver) -> bool:
        """HomePower is currently driving this battery (so the settings it drives are locked in the panel)."""
        return bool(driver.config.extra.get("control_enabled", True)) and not (self.paused or self.safe)

    async def set_device_setting(self, device_id: str, key: str, value) -> dict:
        """Change one setting of a battery from the panel (admin only). Checked against the vendor's own ranges and rules first."""
        from .core.device_settings import apply_change, read_settings

        cached = self._adapters.get(device_id)
        if device_id not in self.devices or cached is None:
            raise ValidationError("unknown battery")
        driver = getattr(cached[1], "driver", None)
        if driver is None:
            raise ValidationError("this battery has no settings that can be changed here")
        controlled = self._controlled(driver)
        st = self.coordinator._snap.devices.get(device_id) if self.coordinator else None
        if hasattr(driver, "read_registers") and st is not None and not (st.actual and st.actual.online):
            raise ValidationError("The battery is not answering right now, so nothing was changed. Try again once it shows online.")
        try:
            family = await apply_change(driver, key, value, controlled)
        except ValueError as err:
            raise ValidationError(str(err)) from err
        except Exception as err:  # noqa: BLE001 - the battery or its cloud said no
            raise ValidationError(f"the battery did not accept it: {err}") from err
        _LOGGER.info("%s: setting %s changed to %r from the panel", device_id, key, value)
        if family == "ecoflow":
            driver._cache, driver._pushed = None, 0.0           # the next read comes from the cloud and shows the change
        if self.coordinator:
            await self.coordinator.async_request_refresh()
        try:
            _, settings, _ = await read_settings(driver, controlled)
        except Exception:  # noqa: BLE001 - the change itself went through
            settings = []
        return {"settings": settings, "pending": family == "ecoflow"}

    async def device_details(self, device_id: str) -> dict:
        """Everything one battery reports, for the panel's details sheet (read-only): the last reading, plus the named
        registers of an Anker battery (read now), or every value an EcoFlow battery has sent."""
        from .core.device_details import actual_fields, addresses, anker_rows, wanted
        from .coordinator import _PRIVATE_KEY

        cached = self._adapters.get(device_id)
        if device_id not in self.devices or cached is None:
            raise ValidationError("unknown battery")
        driver = getattr(cached[1], "driver", None)
        st = self.coordinator._snap.devices.get(device_id) if self.coordinator else None
        out: dict = {"actual": actual_fields(st.actual if st else None), "registers": None, "raw": None, "changes": [], "serial": None,
                     "pv": self.coordinator.pv_view(device_id) if self.coordinator else None}
        if driver is None:
            return out
        x1 = bool(driver.config.extra.get("house_meter"))
        silent = hasattr(driver, "read_registers") and st is not None and not (st.actual and st.actual.online)
        if silent:          # do not send a burst of reads to a battery that is not answering: it is shown from what is already known
            out["error"] = "The battery is not answering right now, so its registers were not read."
        elif hasattr(driver, "read_registers") and hasattr(driver, "scan"):          # an Anker battery: Anker's own register lists
            try:
                packs = None
                if x1:
                    n = (await driver.read_registers([10249])).get("10249")
                    packs = n if isinstance(n, int) and 0 <= n <= 6 else None
                entries = wanted(x1, packs)
                addrs, values = addresses(entries), {}
                rest = [a for a in addrs if a != 32775]            # 0x8007 on its own: in a batch with 32774 a battery without it answers a silent 0
                for i in range(0, len(rest), 64):
                    values.update(await driver.read_registers(rest[i:i + 64]))
                if 32775 in addrs:
                    values.update(await driver.read_registers([32775]))
            except Exception as err:  # noqa: BLE001 - shown in the sheet
                out["error"] = f"could not read the registers: {err}"
            else:
                out["registers"] = anker_rows(entries, values, driver._order, x1)
                out["serial"] = next((r["value"] for r in out["registers"] if r["name"] == "Serial number"), None)
        try:
            from .core.device_settings import read_settings

            if silent:
                out["settings"] = []
            else:
                _, out["settings"], _ = await read_settings(driver, self._controlled(driver))
        except Exception as err:  # noqa: BLE001 - the sheet still shows everything else
            out["settings"], out["settings_error"] = [], f"could not read the settings: {err}"
        quota = getattr(driver, "last_quota", None)
        if isinstance(quota, dict) and quota:
            out["raw"] = {k: v for k, v in quota.items() if not _PRIVATE_KEY.search(k)}
            if out["registers"] is None:
                from .core.device_details import ecoflow_rows

                out["registers"] = ecoflow_rows(out["raw"])      # named the way the vendor's own Home Assistant integration names them
        out["changes"] = list(getattr(driver, "changes", None) or [])[-20:]
        conn = driver.config.extra.get("connection") or {}
        out["serial"] = out["serial"] or conn.get("sn")
        return out

    async def register_note(self, device_id: str, address: int | None = None, label: str | None = None) -> dict:
        """The user's own names for registers (what they found by watching), per battery. A label of "" removes one."""
        if device_id not in self.devices:
            raise ValidationError("unknown battery")
        notes = self.register_notes.setdefault(device_id, {})
        if address is not None:
            label = str(label or "").strip()[:80]
            if label:
                notes[str(int(address))] = label
            else:
                notes.pop(str(int(address)), None)
            await self._save()
        return {"notes": dict(notes)}
