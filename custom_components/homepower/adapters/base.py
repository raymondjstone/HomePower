"""Adapter contract. The controller only talks to this, never to a vendor protocol."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.models import Actual, DeviceConfig, Desired


class DeviceAdapter(ABC):
    def __init__(self, config: DeviceConfig) -> None:
        self.config = config

    @abstractmethod
    async def read(self) -> Actual:
        """Read current state. Must never write."""

    @abstractmethod
    async def apply(self, desired: Desired) -> None:
        """Translate a neutral Desired into vendor commands. Caller verifies by re-reading."""
