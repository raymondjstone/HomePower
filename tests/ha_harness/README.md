# Real Home Assistant checks

These scripts run HomePower against the real Home Assistant core classes (a real `HomeAssistant` object, the real
coordinator/manager, Home Assistant's own form and websocket machinery) and a simulated Anker battery on a real TCP
socket. They are not part of the normal `pytest` run because they need Home Assistant installed:

    pip install homeassistant pytest      # Linux / macOS / WSL; on Windows the classes import and run too
    python tests/ha_harness/run_all.py    # every script below, one line each (also run by GitHub Actions on each push)

Each script imports `_harness.py`, which stops Python 3.12+'s `Server.wait_closed()` waiting for the connection HomePower keeps
open to the simulated battery (without it most scripts hang at the end). The scripts one by one:

    python tests/ha_harness/check_imports.py
    python tests/ha_harness/configure_flows.py
    python tests/ha_harness/check_form_schemas.py   # every Configure / setup form converts to JSON for the browser (Probatio or voluptuous_serialize)
    python tests/ha_harness/e2e_control_loop.py
    python tests/ha_harness/e2e_flows_and_sensors.py
    python tests/ha_harness/e2e_races_and_handback.py
    python tests/ha_harness/e2e_features.py
    python tests/ha_harness/e2e_websocket_api.py
    python tests/ha_harness/e2e_setup_unload.py
    python tests/ha_harness/e2e_new_features.py
    python tests/ha_harness/e2e_weather.py
    python tests/ha_harness/e2e_damaged_state.py
    python tests/ha_harness/e2e_debug_privacy.py
    python tests/ha_harness/e2e_ecoflow_scenario.py
    python tests/ha_harness/e2e_es5000_restart.py
    python tests/ha_harness/e2e_ecoflow_modbus.py   # an ES5000 over local Modbus: charge/discharge in watts, hand-back, restart, reversed-sign detection
    python tests/ha_harness/e2e_multiday.py   # three days across the autumn clock change: cheap-window charging, a 90 % by 07:00 goal, command count
    python tests/ha_harness/e2e_handback_retry.py   # a forced battery is always handed back, even after failures
    python tests/ha_harness/e2e_restore_junk.py   # a damaged or hostile backup file restores safely
    python tests/ha_harness/e2e_soak.py        # a simulated day of cycles: speed and growth (SOAK_CYCLES=... to change)
    python tests/ha_harness/e2e_export_events_repairs.py   # paid export, wear cost, a saving session, Repairs, the hardware report, X1 solar strings
    python tests/ha_harness/e2e_x1_limits_release.py   # removing HomePower puts back the X1 limits it changed (also with the X1 unreachable)
    python tests/ha_harness/e2e_insights.py      # scenes, backup/restore, heads-ups, learned usage, the combined battery and its sensors
    python tests/ha_harness/e2e_solar_room.py    # room for solar: forecast, sunrise/sunset, ceilings, capped grid charging (clock fixed)
    python tests/ha_harness/e2e_flows_diag_details.py   # set-up flow, diagnostics download, actions, switches, Repairs and one battery's registers and settings
    python tests/ha_harness/e2e_offline_and_keys.py   # offline sensors and logging, EcoFlow keys refused
    python tests/ha_harness/e2e_refusals.py   # refused forms, restore and removal
    python tests/ha_harness/e2e_inputs_and_messages.py   # missing or odd prices, forecast, meter; digests and alerts
    python tests/ha_harness/e2e_loop_safety.py   # failing parts of the control loop, Test control, hand-back
    python tests/ha_harness/e2e_setup_failures.py   # setup, unload and the panel API failing
    python tests/ha_harness/e2e_devices.py   # one device per battery on the real registries: upgrade, rename, delete
    python tests/ha_harness/e2e_panel_requests.py   # panel requests: ordering, registers, overrides, probe, control, comparisons
    python tests/ha_harness/e2e_solar_forecast.py   # Home Assistant's real Forecast.Solar platform: hourly forecast, history, the views

What they cover: every module imports and every Home Assistant function used has the signature HomePower calls;
the Configure flow for every battery model; the whole control loop (monitor-only, control, hand-back, override,
pause, test-control); adding/removing batteries mid-cycle; goals, backup mode, peak shaving, savings accounting,
persistence and alerts; and all websocket commands with JSON-serialisability checks. `e2e_new_features.py` covers safe mode (including while paused), the debug file, first-command confirmation, warnings on save, the phone notification buttons, the live view's newer sections, and that the newer state survives a save and reload.
