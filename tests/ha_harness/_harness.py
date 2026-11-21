"""Shared set-up for the real-Home-Assistant checks (imported by each script, after conftest).

Python 3.12 changed asyncio.Server.wait_closed() to wait until every client connection has closed. The scripts stop their
simulated battery with `srv.close(); await srv.wait_closed()` while HomePower still holds its Modbus connection open (as it
does in real use), so on 3.12+ that line never returns. Here it returns once close() has been called, as it did before."""
import asyncio.base_events as _base


async def _wait_closed(self) -> None:
    return None


_base.Server.wait_closed = _wait_closed
