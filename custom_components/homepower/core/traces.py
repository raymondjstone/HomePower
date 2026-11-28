"""A record of what happened after each command to a battery that picks its own charge rate (EcoFlow).

For every Charge or Hold sent, it watches the battery for a few minutes and writes down whether it reacted, how long that
took and the most power seen. After a few of them there is real evidence of whether "raise the reserve" works on that
model. Pure Python (no Home Assistant), so it is unit-tested directly.
"""
from __future__ import annotations
from typing import Any

WINDOW_S = 600          # how long a command is watched
CHARGING_W = 500        # this much power counts as "it is charging"
KEEP = 30               # finished traces kept per battery
UNRELIABLE_AFTER = 3    # this many Charge commands in a row with no effect = it does not work on this battery
NEAR_FULL = 85          # a Charge that had no effect from this charge level up proves nothing (tapering, or the battery's own
                        # charge limit set in its app): it is not counted against the battery


class CommandTrace:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.results: dict[str, list[dict[str, Any]]] = {}
        for did, rows in (data.items() if isinstance(data, dict) else ()):
            if isinstance(rows, list):
                self.results[did] = [r for r in rows if isinstance(r, dict) and isinstance(r.get("mode"), str) and "ok" in r][-KEEP:]
        self._open: dict[str, dict[str, Any]] = {}

    def start(self, did: str, mode: str, now_s: float, at: str, soc: float | None, power_w: float | None) -> None:
        """A command was sent. Charge is only worth watching when there was room to charge: the caller checks that."""
        self._open[did] = {"mode": mode, "t0": now_s, "at": at, "soc0": soc, "peak_w": max(0.0, power_w or 0.0), "soc": soc}

    def observe(self, did: str, now_s: float, soc: float | None, power_w: float | None, mode: str | None) -> dict[str, Any] | None:
        """Look at the battery. Returns the finished record when the watch ends (it reacted, or the window ran out)."""
        t = self._open.get(did)
        if t is None:
            return None
        if soc is not None:
            t["soc"] = soc
        if power_w is not None and power_w > t["peak_w"]:
            t["peak_w"] = float(power_w)
        elapsed = now_s - t["t0"]
        reacted = (t["peak_w"] >= CHARGING_W or (soc is not None and t["soc0"] is not None and soc >= t["soc0"] + 1)
                   if t["mode"] == "charge" else mode == t["mode"])
        if not reacted and elapsed < WINDOW_S:
            return None
        del self._open[did]
        rec = {"at": t["at"], "mode": t["mode"], "ok": bool(reacted), "reaction_s": round(elapsed) if reacted else None,
               "peak_w": round(t["peak_w"]), "soc_before": t["soc0"], "soc_after": t["soc"]}
        if not reacted and t["mode"] == "charge" and t["soc0"] is not None and t["soc0"] >= NEAR_FULL:
            return {**rec, "inconclusive": True}          # reported, not kept: it must not count towards "Charge not working"
        self.results.setdefault(did, []).append(rec)
        del self.results[did][:-KEEP]
        return rec

    def cancel(self, did: str) -> None:
        self._open.pop(did, None)

    def reset(self, did: str, mode: str = "charge") -> None:
        """A fresh test proved it works: forget the failures so Charge is offered again."""
        self.results[did] = [r for r in self.results.get(did, []) if r["mode"] != mode]
        self._open.pop(did, None)

    def verdict(self, did: str, mode: str = "charge") -> str:
        """'works' (it reacted lately), 'unreliable' (several tries, no reaction) or 'unknown' (too little evidence)."""
        rows = [r for r in self.results.get(did, []) if r["mode"] == mode][-5:]
        if any(r["ok"] for r in rows):
            return "works"
        return "unreliable" if len(rows) >= UNRELIABLE_AFTER else "unknown"

    def grace(self, did: str, retry_s: float) -> int:
        """How many retries to wait before a cloud battery counts as not following commands: enough to cover how long it has
        really taken to react (the slowest of its recent reactions), at least one and at most four."""
        times = [r["reaction_s"] for r in self.results.get(did, [])[-10:] if r.get("ok") and isinstance(r.get("reaction_s"), (int, float))]
        if len(times) < 3 or retry_s <= 0:
            return 1
        return max(1, min(4, 1 + int(max(times) // retry_s)))

    def recent(self, did: str, n: int = 6) -> list[dict[str, Any]]:
        return list(self.results.get(did, []))[-n:]

    def forget(self, did: str) -> None:
        self.results.pop(did, None)
        self._open.pop(did, None)

    def dump(self) -> dict[str, Any]:
        return {k: list(v) for k, v in self.results.items()}
