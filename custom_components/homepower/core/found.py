"""Batteries found on the network, matched against the ones HomePower already has (HA-free).

A battery is found two ways: EcoFlow STREAM batteries announce themselves (zeroconf `_ecoflow._tcp.local.`, the service
EcoFlow's own integration listens for), and Anker batteries are found by HomePower's scan of the local network (Anker's
batteries announce nothing). Either way the same question comes first: is this a battery HomePower already has? If so, and
it has moved to another address (a new DHCP lease), the stored address is put right; nothing is offered.
"""
from __future__ import annotations

import re

from .profiles import PROFILE_BY_ID

# Batteries reached at an address on the local network: the address is what discovery can put right.
LOCAL_DRIVERS = ("anker_modbus", "ecoflow_modbus")
VENDOR_DRIVERS = {"anker": ("anker_modbus",), "ecoflow": ("ecoflow_modbus", "ecoflow_stream")}


def norm_serial(value) -> str | None:
    s = str(value or "").strip().upper()
    return s or None


def _driver(dev: dict) -> str:
    return str(dev.get("driver") or (PROFILE_BY_ID.get(str(dev.get("kind"))) or {}).get("driver") or "")


def _serial(dev: dict) -> str | None:
    conn = dev.get("connection") or {}
    return norm_serial(conn.get("serial") or conn.get("sn"))      # sn: an EcoFlow battery added through the cloud API


def match_known(devices: dict[str, dict], vendor: str, host: str, port: int, serial: str | None) -> tuple[str | None, dict | None]:
    """The battery HomePower already has for one found at host:port (with this serial, if it said), and the battery's settings
    to save when they need putting right, else None.

    By serial first: the same battery at another address gets its new address (discovery-update-info). Then by address: a
    battery saved before serials were kept gets its serial, so a later move of address is recognised too.
    """
    serial = norm_serial(serial)
    mine = {did: d for did, d in devices.items() if _driver(d) in VENDOR_DRIVERS.get(vendor, ())}
    if serial:
        for did, d in mine.items():
            if _serial(d) != serial:
                continue
            conn = d.get("connection") or {}
            if _driver(d) in LOCAL_DRIVERS and (conn.get("host"), int(conn.get("port") or 502)) != (host, int(port)):
                return did, {**d, "connection": {**conn, "host": host, "port": int(port)}}
            return did, None
    for did, d in mine.items():
        conn = d.get("connection") or {}
        if _driver(d) in LOCAL_DRIVERS and (conn.get("host"), int(conn.get("port") or 502)) == (host, int(port)):
            if serial and not _serial(d):
                return did, {**d, "connection": {**conn, "serial": serial}}
            if serial and _serial(d) != serial:
                return None, None       # another battery now has this address (the first one moved): it is a new one
            return did, None
    return None, None


def known_hosts(devices: dict[str, dict]) -> set[str]:
    """Addresses of the local batteries HomePower already talks to: the network scan leaves them alone (an Anker battery takes
    one Modbus client at a time, and HomePower is it)."""
    return {str((d.get("connection") or {}).get("host")) for d in devices.values()
            if _driver(d) in LOCAL_DRIVERS and (d.get("connection") or {}).get("host")}


def anker_kinds(x1: bool) -> tuple[list[dict], str | None]:
    """The Anker models to choose from for a found battery, and the one to start on: an X1 says it is one; the Solarbank
    models cannot be told apart from what the scan reads, so the user picks."""
    kinds = [{"value": p["id"], "label": p["label"]} for p in PROFILE_BY_ID.values() if p["driver"] == "anker_modbus"]
    return kinds, ("anker_x1" if x1 else None)


ECOFLOW_LOCAL_KIND = "ecoflow_stream_5000_local"


def new_device_id(name: str, devices: dict) -> str:
    """An id for a battery being added, from its name, that no battery already has (two of the same name must not overwrite
    each other). Letters, digits and _ only, as the options flow makes them."""
    base = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:80] or "battery"
    did, n = base, 2
    while did in devices:
        did, n = f"{base}_{n}", n + 1
    return did


def discovered_name(vendor: str, model: str | None, serial: str | None) -> str:
    """What a found battery is called until the user names it: its model, and the end of its serial to tell two apart."""
    base = (model or "").strip() or ("EcoFlow STREAM" if vendor == "ecoflow" else "Anker battery")
    return f"{base} {serial[-4:]}" if serial and len(serial) >= 4 else base
