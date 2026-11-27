"""Setting up, reloading and the panel's API when parts of Home Assistant (or HomePower) do not cooperate."""
import _home  # noqa: F401 - sets up the paths first
import asyncio
from types import SimpleNamespace
import custom_components.homepower as hp
from custom_components.homepower import websocket_api as H
from custom_components.homepower.core.serde import ValidationError


async def aboom(*a, **kw):
    raise OSError("boom")


def boom(*a, **kw):
    raise OSError("boom")


async def panel_api():
    h = await _home.Home().start()
    hass = h.hass
    sent, errors = [], []
    conn = SimpleNamespace(send_result=lambda i, r=None: sent.append(r), send_error=lambda i, c, m: errors.append(m),
                           send_message=lambda m: sent.append(m), subscriptions={})

    def raw(fn):
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        return fn

    real, H.running_manager = H.running_manager, lambda hass: None
    for call in (lambda: H._mgr(hass), lambda: H._coord(hass)):
        try:
            call()
        except ValidationError as err:
            assert "still starting" in str(err)
        else:
            raise AssertionError("no manager, no answer")
    raw(H.ws_get)(hass, conn, {"id": 1})
    assert errors and "still starting" in errors[-1], errors
    H.running_manager = lambda hass: SimpleNamespace(coordinator=None)
    try:
        H._coord(hass)
    except ValidationError:
        pass
    else:
        raise AssertionError("no control loop yet")
    H.running_manager = real

    # a live view that cannot be drawn is logged, never raised into Home Assistant's dispatcher
    pushes = []
    real_connect, H.async_dispatcher_connect = H.async_dispatcher_connect, lambda hass, signal, fn: pushes.append(fn) or (lambda: None)
    await raw(H.ws_subscribe)(hass, conn, {"id": 2})
    real_view, h.coord.view = h.coord.view, boom
    for push in pushes:
        push()
    h.coord.view, H.async_dispatcher_connect = real_view, real_connect
    print("panel    : refuses while starting; a drawing failure is contained")

    # setting up the panel without the web parts of Home Assistant: said, and scheduling carries on
    notes = []
    real_notify, hp._notify = hp._notify, lambda hass, key, message: notes.append(key)
    await hp._async_setup_ui_api(hass, {})
    done = {"ui_api": True}
    await hp._async_setup_ui_api(hass, done)
    await hp._async_register_panel(SimpleNamespace(data={}))
    hp._notify = real_notify
    assert "panel" in notes, notes
    real_read, hp.Path.read_bytes = hp.Path.read_bytes, lambda self: boom()
    assert hp._asset_tag().startswith(hp.VERSION)
    hp.Path.read_bytes = real_read
    print("setup    : panel failures are reported, not fatal:", notes)
    await h.stop()


async def entry():
    h = await _home.Home().start()
    hass, e = h.hass, h.entry
    stops, timers, unloads, tasks = [], [], [], []
    hass.bus = SimpleNamespace(async_listen_once=lambda event, fn: stops.append(fn) or (lambda: None),
                               async_listen=lambda *a, **kw: (lambda: None))

    async def forward(entry, platforms):
        return True

    hass.config_entries = SimpleNamespace(async_forward_entry_setups=forward, async_unload_platforms=forward,
                                          async_get_known_entry=lambda i: e, async_entries=lambda d=None: [e],
                                          async_update_entry=lambda *a, **kw: True)
    fake_entry = SimpleNamespace(entry_id="e1", options={}, data={}, runtime_data=None, pref_disable_polling=True, add_update_listener=lambda f: (lambda: None),
                                 async_on_unload=unloads.append, state=e.state,
                                 async_create_background_task=lambda hass, coro, name: tasks.append(coro))

    class Coordinator(hp.HomePowerCoordinator):
        fail = False

        async def async_config_entry_first_refresh(self):
            if Coordinator.fail:
                raise OSError("first refresh")

    async def nothing(*a, **kw):
        return None

    real = hp.HomePowerCoordinator, hp._async_setup_ui_api, hp._async_register_panel, hp.async_track_time_interval
    hp.HomePowerCoordinator, hp._async_setup_ui_api, hp._async_register_panel = Coordinator, nothing, nothing
    hp.async_track_time_interval = lambda hass, fn, every: timers.append(fn) or (lambda: None)
    Coordinator.fail = True
    try:
        await hp.async_setup_entry(hass, fake_entry)
    except OSError:
        pass
    else:
        raise AssertionError("a failed first refresh must fail setup, so it is retried")
    Coordinator.fail = False
    assert await hp.async_setup_entry(hass, fake_entry)
    for task in tasks:
        await task
    await timers[-1]()                                  # the daily safety copy
    real_job, hass.async_add_executor_job = hass.async_add_executor_job, aboom
    await timers[-1]()                                  # ... which never affects control when it fails
    hass.async_add_executor_job = real_job
    await stops[-1](None)                               # Home Assistant stopping: batteries are handed back
    rd = fake_entry.runtime_data
    rd.coordinator.release_all = aboom
    assert await hp.async_unload_entry(hass, fake_entry)      # unloading finishes even if the hand-back does not
    hp.HomePowerCoordinator, hp._async_setup_ui_api, hp._async_register_panel, hp.async_track_time_interval = real
    print("entry    : retried when the first read fails; daily copy; stop and unload")


async def removal_with_damaged_battery():
    h = await _home.Home().start()
    mgr = h.mgr
    mgr.devices = {"bad": {"device_id": "bad", "driver": "anker_modbus"}, "a": mgr.devices["a"]}
    mgr.event_state.update({"x1lim:bad:export": 500, "x1lim:a:export": 500})
    await mgr.save_runtime()
    real_manager = hp.HomePowerManager

    class Loaded(real_manager):
        async def async_load(self):
            self.devices, self.event_state = dict(mgr.devices), dict(mgr.event_state)

    hp.HomePowerManager = Loaded
    from custom_components.homepower.drivers.anker import AnkerModbusDriver
    real_close, AnkerModbusDriver.close = AnkerModbusDriver.close, aboom
    await hp._release_x1_limits_on_remove(h.hass)        # a damaged row and a connection that will not close
    AnkerModbusDriver.close, hp.HomePowerManager = real_close, real_manager
    print("removal  : a damaged battery does not stop it")
    await h.stop()


async def small_parts():
    h = await _home.Home().start()
    coord = h.coord
    from custom_components.homepower import coord_forecast as CF
    from homeassistant.helpers import sun
    now = hp.timedelta
    from homeassistant.util import dt as dt_util
    today = dt_util.now()
    real = sun.get_astral_event_date
    sun.get_astral_event_date = lambda *a: None                       # a polar day: no sunrise
    assert coord._sun(today.date()) is None
    sun.get_astral_event_date = boom
    assert coord._sun(today.date()) is None
    coord.apply_settings({"solar_room": True})
    coord.adapters[0].config.extra["solar"] = "yes"
    assert coord._solar_ceilings(today, {}, None) == {} and coord.solar_room.get("missing") == "sun", coord.solar_room
    sun.get_astral_event_date = real
    h.mgr.forecast_days["2000-01-01"] = 1.0
    coord.apply_settings({"forecast_entity": "sensor.fc"})
    h.hass.states.async_set("sensor.fc", "5")
    coord._forecast_as_given(today)
    assert "2000-01-01" not in h.mgr.forecast_days
    print("forecast : no sun is not an error; old days are forgotten")
    await h.stop()


async def main():
    await panel_api()
    await entry()
    await removal_with_damaged_battery()
    await small_parts()
    print("E2E SETUP FAILURES OK")


asyncio.run(main())
