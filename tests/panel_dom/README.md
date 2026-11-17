# Panel in a real DOM

Loads the real panel (all of its parts, with a `?v=` tag like Home Assistant does) in jsdom, feeds it the payloads the real
backend produces, renders every tab and presses every button. Not part of the normal `pytest` run: it needs Home Assistant
(for the payloads) and jsdom.

    pip install homeassistant
    npm install jsdom                       # in this folder
    python tests/ha_harness/dump_panel_payload.py tests/panel_dom/payload.json
    node tests/panel_dom/panel_dom.mjs      # every tab renders; every button works
    node tests/panel_dom/save_warn.mjs      # "Saved, but note: ..." after saving a schedule

`payload.json` and `node_modules` are generated: do not commit them.
