# Real Home Assistant checks

These scripts run HomePower against the real Home Assistant core classes (a real `HomeAssistant` object, the real
coordinator/manager, Home Assistant's own form and websocket machinery) and a simulated Anker battery on a real TCP
socket. They are not part of the normal `pytest` run because they need Home Assistant installed:

    pip install homeassistant pytest      # Linux / macOS / WSL; on Windows the classes import and run too
    python tests/ha_harness/check_imports.py
    python tests/ha_harness/configure_flows.py
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

What they cover: every module imports and every Home Assistant function used has the signature HomePower calls;
the Configure flow for every battery model; the whole control loop (monitor-only, control, hand-back, override,
pause, test-control); adding/removing batteries mid-cycle; goals, backup mode, peak shaving, savings accounting,
persistence and alerts; and all websocket commands with JSON-serialisability checks. `e2e_new_features.py` covers safe mode (including while paused), the debug file, first-command confirmation, warnings on save, the phone notification buttons, the live view's newer sections, and that the newer state survives a save and reload.
