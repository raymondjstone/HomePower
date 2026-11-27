"""Shared set-up for the real-Home-Assistant checks (imported by each script, after conftest).

Python 3.12 changed asyncio.Server.wait_closed() to wait until every client connection has closed. The scripts stop their
simulated battery with `srv.close(); await srv.wait_closed()` while HomePower still holds its Modbus connection open (as it
does in real use), so on 3.12+ that line never returns. Here it returns once close() has been called, as it did before."""
import asyncio.base_events as _base


async def _wait_closed(self) -> None:
    return None


_base.Server.wait_closed = _wait_closed


def attach(hass, entry, mgr):
    """Make `mgr` the running HomePower the way async_setup_entry does: HomePowerData on the entry's runtime_data, the entry
    loaded. These scripts swap hass.config_entries for small stand-ins as they go, so the lookup also finds this entry
    for this hass; the entry's state and runtime_data are still checked as in the real lookup."""
    from homeassistant.config_entries import ConfigEntryState
    from custom_components.homepower import runtime

    entry.runtime_data = runtime.HomePowerData(mgr, mgr.coordinator)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    real = getattr(runtime, "_real_running", runtime.running)
    runtime._real_running = real

    def running(h):
        found = real(h)
        if found is None and h is hass and entry.state in runtime._RUNNING and isinstance(getattr(entry, "runtime_data", None), runtime.HomePowerData):
            return entry.runtime_data
        return found
    runtime.running = running
