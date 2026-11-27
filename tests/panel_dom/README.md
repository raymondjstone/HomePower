# Panel in a real DOM

Loads the real panel (all of its parts, with a `?v=` tag like Home Assistant does) in jsdom, feeds it the payloads the real
backend produces, renders every tab and presses every button. Not part of the normal `pytest` run: it needs Home Assistant
(for the payloads) and jsdom. CI runs all of these except the browser test (the `panel` job in `.github/workflows/tests.yml`).

    pip install homeassistant
    npm install jsdom@29.1.1                # in this folder (the version CI uses)
    python tests/ha_harness/dump_panel_payload.py tests/panel_dom/payload.json
    node tests/panel_dom/panel_dom.mjs      # every tab renders; every button works
    node tests/panel_dom/save_warn.mjs      # "Saved, but note: ..." after saving a schedule
    node tests/panel_dom/link_states.mjs    # "Readings stale" / "Waiting for readings" / "Offline" on every tab and the details sheet
    node tests/panel_dom/solar_forecast.mjs # today's solar forecast tile, and forecast against measured solar in Insights
    node tests/panel_dom/field_labels.mjs   # fields all named for screen readers, no duplicate ids, every action keyboard-reachable
    node tests/panel_dom/render_fuzz.mjs    # every tab drawn 3000 times with values removed at random: never blank
    node tests/panel_dom/card.mjs           # the Lovelace card: finds its sensor, escapes names, redraws only on its own changes
    node tests/panel_dom/battery_form.mjs   # the add / edit battery form for the local EcoFlow profile
    node tests/panel_dom/reattach.mjs       # the panel survives Home Assistant detaching and re-attaching it
    node tests/panel_dom/override_monitor.mjs   # monitor-only battery: cannot be picked for an override, and its plan row is marked
    node tests/panel_dom/settings_error.mjs     # a refused Settings save names the field by its label, not its key
    node tests/panel_dom/report_issue.mjs   # Report an issue: versions and battery types filled in, nothing personal
    node tests/panel_dom/audit_ui.mjs       # an odd currency symbol is not injected, a refused delete stays on screen, a failed load recovers

    npm install playwright && npx playwright install chromium
    node tests/panel_dom/panel_browser.mjs  # the same in real headless Chromium: module loading with the ?v= tag, console errors,
                                            # every tab, real clicks, and a screenshot of each tab in tests/panel_dom/shots/

    python tests/ha_harness/dump_demo_payload.py tests/panel_dom/demo.json
    npm install --no-save @mdi/js
    node tests/panel_dom/readme_shots.mjs   # the screenshots in the README (docs/images/), from a made-up home

`payload.json`, `demo.json`, `shots/` and `node_modules` are generated: do not commit them. The screenshots have no Home Assistant theme, so the header text is white on white and entity pickers (`ha-entity-picker`) are empty: that is the test page, not the panel.
