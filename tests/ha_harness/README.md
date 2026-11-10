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

What they cover: every module imports and every Home Assistant function used has the signature HomePower calls;
the Configure flow for every battery model; the whole control loop (monitor-only, control, hand-back, override,
pause, test-control); adding/removing batteries mid-cycle; goals, backup mode, peak shaving, savings accounting,
persistence and alerts; and all websocket commands with JSON-serialisability checks.
