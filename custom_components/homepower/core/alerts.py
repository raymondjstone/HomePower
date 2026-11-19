"""Debounced alert tracking: raise once when a problem persists, clear once when it ends."""
from __future__ import annotations


class AlertTracker:
    def __init__(self) -> None:
        self._since: dict[str, float] = {}
        self._raised: set[str] = set()

    def observe(self, key: str, active: bool, now_s: float, after_s: float = 0.0) -> str | None:
        """Returns "raised" / "cleared" on a transition, else None. A problem must last `after_s`
        seconds before it is raised, so a blip does not notify."""
        if active:
            first = self._since.setdefault(key, now_s)
            if key not in self._raised and now_s - first >= after_s:
                self._raised.add(key)
                return "raised"
            return None
        self._since.pop(key, None)
        if key in self._raised:
            self._raised.discard(key)
            return "cleared"
        return None

    def active(self) -> set[str]:
        return set(self._raised)

    def forget(self, key: str) -> list[str]:
        """Drop one alert (an exact key), or every alert under a prefix that ends in ":". Returns the raised ones dropped, so the
        caller can dismiss their notifications. (An exact match matters: "offline:garage" must not also drop "offline:garage_2".)"""
        hit = [k for k in set(self._since) | self._raised if k == key or (key.endswith(":") and k.startswith(key))]
        dropped = [k for k in hit if k in self._raised]
        for k in hit:
            self._since.pop(k, None)
            self._raised.discard(k)
        return dropped
