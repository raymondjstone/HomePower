"""Adapter around a direct driver (Anker Modbus / EcoFlow API): no other integration involved."""
from __future__ import annotations

from ..core.models import Actual, Desired, DeviceConfig
from .base import DeviceAdapter


class DirectAdapter(DeviceAdapter):
    def __init__(self, config: DeviceConfig, driver) -> None:
        super().__init__(config)
        self.driver = driver

    async def read(self) -> Actual:
        return await self.driver.read()

    async def apply(self, desired: Desired) -> None:
        await self.driver.apply(desired)

    async def keepalive(self) -> None:
        fn = getattr(self.driver, "keepalive", None)
        if fn:
            await fn()

    async def close(self) -> None:
        await self.driver.close()
