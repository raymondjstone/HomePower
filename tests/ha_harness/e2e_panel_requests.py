"""The panel's requests that nothing else makes: ordering, registers, backups, overrides, probing, control, comparisons."""
import _home  # noqa: F401 - sets up the paths first
import asyncio
from custom_components.homepower import websocket_api as H
from custom_components.homepower.core.serde import ValidationError


def raw(fn):
    """The handler's own code, without Home Assistant's decorators (admin check, scheduling)."""
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


async def main():
    h = await _home.Home().start(batteries=("a", "b"))
    hass, mgr, coord = h.hass, h.mgr, h.coord

    async def call(handler, **msg):
        got = {}

        class Connection:
            def send_result(self, i, result=None):
                got["result"] = result

            def send_error(self, i, code, message):
                got["error"] = message

        await raw(handler)(hass, Connection(), {"id": 1, **msg})
        if "error" in got:
            raise ValidationError(got["error"])
        return got["result"]

    async def refused(handler, words, **msg):
        try:
            await call(handler, **msg)
        except ValidationError as err:
            assert words in str(err), (words, str(err))
        else:
            raise AssertionError("not refused: " + words)

    await call(H.ws_reorder_devices, device_ids=["b", "a"])
    assert list(mgr.devices) == ["b", "a"]
    assert (await call(H.ws_scan_registers, device_id="a"))["input"]
    assert isinstance((await call(H.ws_backups))["backups"], list)

    await call(H.ws_override, device_ids=[], mode="hold", minutes=5)
    assert coord.view()["devices"]["a"]["override"]
    await refused(H.ws_override, "typo", device_ids=["typo"], mode="hold", minutes=5)

    async def discover(region, access_key, secret_key):
        return [{"sn": "SN1"}]

    mgr.discover_ecoflow = discover
    assert await call(H.ws_discover_ecoflow, region="eu", access_key="a", secret_key="b") == {"devices": [{"sn": "SN1"}]}
    port = mgr.devices["a"]["connection"]["port"]
    assert (await call(H.ws_probe, device={"kind": "anker_e5000_pro", "connection": {"host": "127.0.0.1", "port": port}}))["soc"] == 62

    await call(H.ws_control, device_id="a", enabled=False)
    assert mgr.devices["a"]["control_enabled"] is False
    await refused(H.ws_control, "unknown battery", device_id="nope", enabled=True)

    assert "days" in await call(H.ws_solar_forecast, days=7)
    assert "tariffs" in await call(H.ws_compare_tariffs, days=7)

    def broken(days):
        raise ValueError("'A': peak is not a number")

    coord.tariff_comparison = broken
    await refused(H.ws_compare_tariffs, "peak is not a number", days=7)
    print("requests : ordering, registers, overrides, probe, control, comparisons")
    await h.stop()
    print("E2E PANEL REQUESTS OK")


asyncio.run(main())
