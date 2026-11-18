"""A simulated EcoFlow battery through the whole real control loop, with a clock the test controls:
first-command approval, Charge by raising the reserve, command log, learned charge rate, handing back the user's own reserve,
and giving up on Charge for a battery that ignores the reserve."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, time as real_time, tempfile
import conftest  # noqa: F401
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
import custom_components.homepower.coordinator as C
import custom_components.homepower.drivers.ecoflow as E
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower.core.models import Mode
from custom_components.homepower.core.serde import device_from_dict, device_to_dict


class Clock:
    """Stands in for the time module in the coordinator and the EcoFlow driver."""
    now = 1_000_000.0

    def monotonic(self): return self.now
    def time(self): return self.now
    def strftime(self, fmt): return real_time.strftime(fmt, real_time.localtime(self.now))


import custom_components.homepower.manager as M
M.async_get_clientsession = lambda hass: None        # no real network in this test: the fake API below replaces it
CLOCK = Clock()
C._time = CLOCK
E.time = CLOCK


class FakeEco:
    """An Ultra X: charges from the grid at 2800 W while its backup reserve is above its charge level (if it obeys)."""

    def __init__(self, soc, reserve, obeys=True, capacity_wh=5000):
        self.soc, self.reserve, self.obeys, self.cap, self.power, self.sent = float(soc), reserve, obeys, capacity_wh, 0.0, []

    def tick(self, seconds):
        if self.obeys and self.reserve > self.soc + 1:
            self.power = 2800.0
            self.soc = min(float(self.reserve), self.soc + self.power * seconds / 3600 / self.cap * 100)
        else:
            self.power = 0.0

    async def quota(self, sn):
        return {"soc": round(self.soc, 1), "cmsBattSoc": 0.0, "backupReverseSoc": self.reserve, "powGetBpCms": self.power, "powGetSysGrid": 300.0}

    async def command(self, sn, params, cmd_id=17):
        self.sent.append(params)
        if "cfgBackupReverseSoc" in params:
            self.reserve = int(params["cfgBackupReverseSoc"])


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    hass.data["homepower"] = {"manager": mgr}

    fakes = {"good": FakeEco(40, 30), "deaf": FakeEco(40, 30, obeys=False)}
    for did in fakes:
        raw = {"device_id": did, "name": did.title(), "kind": "ecoflow_stream_ultra_x", "control_enabled": True, "capacity_wh": 5000,
               "max_charge_w": 2300, "max_discharge_w": 0, "min_soc": 10,
               "connection": {"sn": did.upper() + "SN01", "access_key": "ak" + did, "secret_key": "sk" + did, "region": "eu"}}
        mgr.devices[did] = device_to_dict(device_from_dict(raw))
    coord.set_config(*mgr.build())
    assert len(coord.adapters) == 2, [a.config.device_id for a in coord.adapters]
    ad = {a.config.device_id: a for a in coord.adapters}
    for did, a in ad.items():
        a.driver.api = fakes[did]

    async def cycles(n, step=10.0):
        for _ in range(n):
            CLOCK.now += step
            for f in fakes.values():
                f.tick(step)
            await coord.async_refresh()

    def st(did): return coord._snap.devices[did]
    def charge_cmds(did): return [p for p in fakes[did].sent if "cfgBackupReverseSoc" in p and p["cfgBackupReverseSoc"] > 40]

    # a schedule that charges both to 80 % all day
    await mgr.save_schedule({"id": "Night", "start": "00:00", "end": "23:59", "mode": "charge", "power_w": 2000, "target_soc": 80})
    await cycles(4)
    assert st("good").awaiting_confirm and not fakes["good"].sent, (st("good").awaiting_confirm, fakes["good"].sent)
    print("1 approval : first command is waiting for the user, nothing was sent")

    await mgr.confirm_first_command("good")
    await mgr.confirm_first_command("deaf")
    await cycles(14)                                        # 140 s: command goes out, battery charges, log records it
    g = fakes["good"]
    assert charge_cmds("good") and charge_cmds("good")[0] == {"cfgBackupReverseSoc": 80}, g.sent
    assert g.soc > 41 and g.power == 2800.0
    assert coord.traces.results.get("good") and coord.traces.results["good"][-1]["ok"] is True, coord.traces.results
    assert coord.rate_learner.rates.get("good") in (2800, 2790, 2810), coord.rate_learner.rates
    assert coord.traces.verdict("good") == "works"
    print("2 charging : reserve raised to 80, log says it worked, learned rate", coord.rate_learner.rates["good"], "W")

    live = coord.view()["devices"]["good"]
    assert live["measured_charge_w"] and live["report"]["worked"] >= 1 and live["why"] is None, live
    assert coord._effective_config(ad["good"].config).caps.max_charge_w == coord.rate_learner.rates["good"]
    print("3 view     : report and measured rate are shown;", live["report"]["advice"])

    # plan no longer wants it: its own reserve (30) comes back, not the minimum (10)
    await mgr.delete_schedule("Night")
    await cycles(30)
    assert g.reserve == 30, g.reserve
    assert not st("good").release_pending
    print("4 hand back: reserve restored to the user's own", g.reserve, "%")

    # the battery that ignores the reserve: three Charge commands with no effect -> Charge is dropped for it
    d = fakes["deaf"]
    for night in range(3):
        await mgr.save_schedule({"id": "Night", "start": "00:00", "end": "23:59", "mode": "charge", "power_w": 2000, "target_soc": 80})
        await cycles(75)                                    # 12.5 minutes: past the 10-minute watch
        await mgr.delete_schedule("Night")
        await cycles(30)
    res = coord.traces.results["deaf"]
    assert [r["ok"] for r in res if r["mode"] == "charge"] == [False, False, False], res
    assert coord.traces.verdict("deaf") == "unreliable"
    assert Mode.CHARGE not in coord._effective_config(ad["deaf"].config).caps.supported_modes
    assert d.reserve == 30, d.reserve
    await mgr.save_schedule({"id": "Night", "start": "00:00", "end": "23:59", "mode": "charge", "power_w": 2000, "target_soc": 80})
    before, charges_before = len(d.sent), len(charge_cmds("deaf"))
    await cycles(20)
    print("   notes:", coord.last_result.notes[-1:], "| desired:", st("deaf").desired)
    assert len(d.sent) == before and len(charge_cmds("deaf")) == charges_before, "a command was sent after Charge was judged not to work"
    assert any("not supported" in n for n in coord.last_result.notes)
    assert coord.view()["devices"]["deaf"]["charge_verdict"] == "unreliable"
    print("5 fallback : three failed Charge commands -> Charge dropped, no more sent;", coord.view()["devices"]["deaf"]["report"]["advice"][:60])
    # the good one is still fine
    assert coord.traces.verdict("good") == "works"
    await mgr.delete_schedule("Night")
    await cycles(30)

    # ---- Test control on batteries that pick their own rate: Hold first, then Charge, then report what really happened
    class Shim:                                              # asyncio.sleep advances the test clock instead of waiting
        def __getattr__(self, n): return getattr(asyncio, n)
        async def sleep(self, secs):
            CLOCK.now += secs
            for f in fakes.values():
                f.tick(secs)
            await asyncio.sleep(0)
    real_asyncio, C.asyncio = C.asyncio, Shim()
    try:
        r_good = await coord.test_control("good")
        r_deaf = await coord.test_control("deaf")
    finally:
        C.asyncio = real_asyncio
    names = [x["name"] for x in r_good["steps"]]
    assert r_good["ok"] and names == ["control allowed", "battery readable", "sent hold", "sent charge", "battery actually charged", "handed back"], r_good
    assert r_good["steps"][0]["detail"] == "" and r_good["steps"][1]["detail"] == "", r_good      # a passing step shows no failure hint
    assert [x for x in r_good["steps"] if x["name"] == "battery actually charged"][0]["ok"] is True
    charged = [x for x in r_deaf["steps"] if x["name"] == "battery actually charged"][0]
    assert charged["ok"] is False and charged["info"] and "ignore the reserve" in charged["detail"], charged
    assert r_deaf["ok"] is True                                # the pass/fail result does not depend on the info line
    assert fakes["good"].reserve == 30 and fakes["deaf"].reserve == 30, (fakes["good"].reserve, fakes["deaf"].reserve)   # both handed back
    assert coord.traces.verdict("deaf") == "unreliable"       # a test that saw no charging does not clear the evidence
    print("6 test     : good ->", [x["name"] for x in r_good["steps"] if x["ok"]][-3:], "| deaf ->", charged["detail"][:55])

    # ---- the setup check names the first-command step until it is allowed
    mgr.confirmed.discard("good"); mgr.tests.pop("good", None)
    item = [i for i in coord.setup_check() if i["key"] == "first:good"]
    assert item and item[0]["ok"] is False and "Allow first command" in item[0]["detail"], item
    mgr.confirmed.add("good")
    assert [i for i in coord.setup_check() if i["key"] == "first:good"][0]["ok"] is True
    print("7 setup    : the checklist shows the first-command step")

    # ---- daily command cap: stops Hold/Charge commands, never the hand-back
    from custom_components.homepower.core.sanity import CommandBudget
    coord.budget = CommandBudget(1)                           # a fresh day with a limit of one command
    g = fakes["good"]
    await mgr.save_schedule({"id": "Flap", "start": "00:00", "end": "23:59", "mode": "charge", "power_w": 2000, "target_soc": 95})
    await cycles(14)
    assert g.reserve == 95, g.reserve                          # the one command the day allows: it is charging now
    await mgr.save_schedule({"id": "Flap", "start": "00:00", "end": "23:59", "mode": "hold"})          # a different command: the cap refuses it
    await cycles(14)
    assert g.reserve == 95 and st("good").last_error and "Daily limit of 1" in st("good").last_error, (g.reserve, st("good").last_error)
    sent_now = len(g.sent)
    await mgr.delete_schedule("Flap")
    await cycles(30)
    assert g.reserve == 30 and len(g.sent) > sent_now, "the hand-back must still go through at the cap"
    print("8 cap      : the second command was refused (reserve stayed 95); the hand-back still restored", g.reserve, "%")
    coord.budget = CommandBudget()

    # ---- EcoFlow refuses commands (error 1006) then rate limits: friendly message, back-off, no hammering
    from custom_components.homepower.drivers.ecoflow import EcoFlowError
    flaky = fakes["good"]
    calls = []

    async def refuse(sn, params, cmd_id=17):
        calls.append(params)
        err = EcoFlowError("EcoFlow error 1006: not supported")
        err.code = "1006"
        raise err
    flaky.command = refuse
    st("good").last_error = None
    await mgr.save_schedule({"id": "Night", "start": "00:00", "end": "23:59", "mode": "charge", "power_w": 2000, "target_soc": 80})
    await cycles(30)
    assert st("good").last_error and "1006" in st("good").last_error and "HomePower will not retry" in st("good").last_error, st("good").last_error
    n_calls = len(calls)
    assert 1 <= n_calls <= 2, n_calls
    await cycles(10)                                          # still inside the five-minute back-off: no new attempts
    assert len(calls) == n_calls, (len(calls), n_calls)
    assert any(k.startswith("error:good") for k in coord.alerts.active())
    print("9 refusal  : one refusal, a clear message,", n_calls, "attempt(s), then it backs off")
    await mgr.delete_schedule("Night")

    # ---- a notification to a phone carries the buttons
    got = []

    async def phone(call):
        got.append(dict(call.data))
    hass.services.async_register("notify", "mobile_app_phone", phone)
    coord.apply_settings({"notify_service": "notify.mobile_app_phone"})
    await coord._notify("stuck:good", "Good was told to charge but is not charging", True)
    await coord._notify("stuck:good", "Good was told to charge but is not charging", False)
    assert got[0]["data"]["actions"] == [{"action": "HOMEPOWER_PAUSE", "title": "Pause HomePower"}, {"action": "HOMEPOWER_HAND_BACK", "title": "Hand back to Auto"}], got
    assert "data" not in got[1] and got[1]["message"].startswith("Resolved:"), got[1]
    print("10 phone   : the alert has Pause and Hand back buttons; the resolved message does not")
    await coord.async_close()
    print("E2E11 OK")


asyncio.run(main())
