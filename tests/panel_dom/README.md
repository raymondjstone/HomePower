# Panel in a real DOM

Loads the real panel (all of its parts, with a `?v=` tag like Home Assistant does) in jsdom, feeds it the payloads the real
backend produces, renders every tab and presses every button. Not part of the normal `pytest` run: it needs Home Assistant
(for the payloads) and jsdom.

    pip install homeassistant
    npm install jsdom                       # in this folder
    python tests/ha_harness/dump_panel_payload.py tests/panel_dom/payload.json
    node tests/panel_dom/panel_dom.mjs      # every tab renders; every button works
    node tests/panel_dom/save_warn.mjs      # "Saved, but note: ..." after saving a schedule

    npm install playwright && npx playwright install chromium
    node tests/panel_dom/panel_browser.mjs  # the same in real headless Chromium: module loading with the ?v= tag, console errors,
                                            # every tab, real clicks, and a screenshot of each tab in tests/panel_dom/shots/

`payload.json`, `shots/` and `node_modules` are generated: do not commit them. The screenshots have no Home Assistant theme, so the header text is white on white and entity pickers (`ha-entity-picker`) are empty: that is the test page, not the panel.
