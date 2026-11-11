import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
import asyncio, sys
from types import SimpleNamespace
import conftest  # noqa: F401  (aliases hp.* so tests/test_drivers imports)
from test_drivers import FakeAnker  # simulated Anker device
from custom_components.homepower.options_flow import HomePowerOptionsFlow
from custom_components.homepower.core.serde import device_from_dict, device_to_dict, ValidationError
from custom_components.homepower.drivers import discovery
from custom_components.homepower.drivers.anker import AnkerModbusDriver


class FakeMgr:
    """Same behaviour as HomePowerManager for the parts the flow uses."""
    def __init__(self, port):
        self.port, self.devices, self.schedules, self.goals = port, {}, {}, {}

    async def discover_anker(self):
        return await discovery.scan(["127.0.0.1"], port=self.port)

    async def discover_ecoflow(self, region, access_key, secret_key):
        if secret_key == "bad":
            raise ValidationError("EcoFlow error 8521: signature is wrong")
        return [{"sn": "BK61ABC", "name": "Garage Ultra X", "online": True, "product": "STREAM Ultra X"}]

    async def probe(self, raw):
        conn = dict(raw["connection"])
        if raw["kind"].startswith("ecoflow"):
            return {"soc": 55, "power_w": -200, "model": "STREAM Ultra X", "capacity_wh": None, "max_charge_w": None, "max_discharge_w": None}
        cfg = device_from_dict({"capacity_wh": 5000, "max_charge_w": 1000, "max_discharge_w": 1000, "device_id": "p", "name": "p", **raw})
        d = AnkerModbusDriver(cfg)
        try:
            return await d.probe()
        except Exception as e:
            raise ValidationError(f"could not read the battery: {e}")
        finally:
            await d.close()

    async def save_schedule(self, raw):
        from custom_components.homepower.core.serde import schedule_from_dict, schedule_to_dict
        sc = schedule_to_dict(schedule_from_dict(raw)); self.schedules[sc["id"]] = sc

    async def save_device(self, raw):
        d = device_to_dict(device_from_dict(raw)); self.devices[d["device_id"]] = d


def mkflow(mgr):
    f = HomePowerOptionsFlow()
    f.hass = SimpleNamespace(data={"homepower": {"manager": mgr}}, states=SimpleNamespace(get=lambda e: None),
                             config_entries=SimpleNamespace(async_get_known_entry=lambda i: SimpleNamespace(options={})))
    f.flow_id, f.handler = "x", "entry"
    return f


async def main():
    dev = FakeAnker()
    srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    mgr = FakeMgr(port)

    # ---- Anker
    f = mkflow(mgr)
    r = await f.async_step_add_battery({"name": "Garage E5000", "kind": "anker_e5000_pro"})
    assert r["step_id"] == "anker_find", r
    print("scan text :", r["description_placeholders"]["found"])
    r = await f.async_step_anker_find(r["data_schema"]({"host": "127.0.0.1", "port": port}))
    assert r["step_id"] == "battery_details", r
    print("readings  :", r["description_placeholders"]["readings"])
    print("asks for  :", [str(k) for k in r["data_schema"].schema])   # only control: the battery supplied the rest
    vals = r["data_schema"]({})
    r = await f.async_step_battery_details(vals)
    assert r["type"] == "create_entry", r
    saved = mgr.devices["garage_e5000"]
    print("saved     :", saved["driver"], saved["connection"]["host"], {k: saved[k] for k in ("capacity_wh","max_charge_w","max_discharge_w","min_soc","control_enabled")})

    # ---- Anker failure path (nothing listening)
    f = mkflow(mgr)
    await f.async_step_add_battery({"name": "Nope", "kind": "anker_x1"})
    r = await f.async_step_anker_find({"host": "127.0.0.1", "port": 1})
    print("failure   :", r["type"], r["errors"], "|", r["description_placeholders"]["error"][:60])

    # ---- EcoFlow
    f = mkflow(mgr)
    r = await f.async_step_add_battery({"name": "Shed", "kind": "ecoflow_stream_ultra_x"})
    assert r["step_id"] == "ecoflow_account"
    r = await f.async_step_ecoflow_account({"region": "eu", "access_key": "AK", "secret_key": "bad"})
    print("eco bad   :", r["errors"], "|", r["description_placeholders"]["error"])
    r = await f.async_step_ecoflow_account({"region": "eu", "access_key": "AK", "secret_key": "SK"})
    assert r["step_id"] == "ecoflow_device"
    r = await f.async_step_ecoflow_device(r["data_schema"]({}))
    assert r["step_id"] == "battery_details"
    r = await f.async_step_battery_details(r["data_schema"]({"capacity_wh": 5000}))
    print("eco saved :", r["type"], mgr.devices["shed"]["connection"]["sn"], mgr.devices["shed"]["supported_modes"])

    # ---- schedules: add, then change one through the edit form (HA's own schema validation on both)
    f = mkflow(mgr)
    r = await f.async_step_add_schedule()
    r = await f.async_step_add_schedule(r["data_schema"]({"name": "Sun", "mode": "charge", "start": "10:00:00", "end": "15:00:00",
                                                          "power_w": 2000, "source": "solar", "target_soc": 95}))
    assert mgr.schedules["Sun"]["source"] == "solar" and mgr.schedules["Sun"]["target_soc"] == 95
    mgr.schedules["Sun"].update(priority=3, total_power_w=4000)            # things only the panel can set
    f = mkflow(mgr)
    r = await f.async_step_init()
    assert "edit_schedule" in r["menu_options"]
    r = await f.async_step_edit_schedule()
    r = await f.async_step_edit_schedule(r["data_schema"]({"schedule": "Sun"}))
    assert r["step_id"] == "edit_schedule_form" and r["description_placeholders"]["name"] == "Sun"
    shown = {str(k): k.default() if callable(getattr(k, "default", None)) else None for k in r["data_schema"].schema}
    assert shown["mode"] == "charge" and shown["source"] == "solar" and shown["start"] == "10:00:00", shown
    r = await f.async_step_edit_schedule_form(r["data_schema"]({"mode": "charge", "start": "11:00:00", "end": "16:00:00",
                                                               "source": "both", "power_w": 2500, "enabled": False}))
    sc = mgr.schedules["Sun"]
    print("sched edit:", r["type"], sc["start"], sc["source"], sc["power_w"], sc["target_soc"], sc["priority"], sc["enabled"])
    assert (sc["start"], sc["end"], sc["source"], sc["power_w"], sc["enabled"]) == ("11:00", "16:00", "both", 2500, False)
    assert sc["target_soc"] is None, "a cleared optional field is cleared"
    assert sc["priority"] == 3 and sc["total_power_w"] == 4000, "fields the form does not show are kept"

    srv.close(); await srv.wait_closed()
    print("ALL FLOWS OK")

asyncio.run(main())
