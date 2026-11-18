"""Plain-language checks and summaries built from what HomePower already knows. No Home Assistant imports."""
from __future__ import annotations

from datetime import time
from math import sqrt
from statistics import mean


def ecoflow_report(results: list[dict], match_pct: int | None, rate_w: int | None, verdict: str) -> dict:
    """A one-glance summary of how well control of a battery that picks its own rate has worked (from the command log)."""
    sent = len(results)
    worked = sum(1 for r in results if r.get("ok"))
    times = [r["reaction_s"] for r in results if r.get("ok") and isinstance(r.get("reaction_s"), (int, float))]
    avg = round(mean(times)) if times else None
    if sent < 3:
        advice = "Not enough commands yet: leave control on for a few days, then check back."
    elif verdict == "unreliable":
        advice = ("Charge is not working on this battery, so HomePower no longer sends it Charge (schedules that ask for it leave the battery on its own control). Check its app (operating mode, backup reserve), "
                  "or leave it on its own control.")
    elif worked / sent >= 0.8 and (match_pct is None or match_pct >= 70):
        advice = "Control looks reliable: it reacts to commands and follows the plan."
    elif worked / sent >= 0.8:
        advice = "It reacts to commands but does not follow the plan well: something else (its app or a schedule on the battery) may be overriding it."
    else:
        advice = "Mixed results: some commands had no effect. Keep an eye on it, and run Test control again."
    return {"sent": sent, "worked": worked, "avg_reaction_s": avg, "charge_rate_w": rate_w, "follows_plan_pct": match_pct,
            "verdict": verdict, "advice": advice}


def _name(d: dict) -> str:
    return d.get("name") or d.get("device_id") or "a battery"


def schedule_warnings(s: dict, devices: list[dict]) -> list[str]:
    """Things worth telling the user about right after they save a schedule (it is still saved)."""
    by_id = {d["device_id"]: d for d in devices}
    picked = [by_id[i] for i in (s.get("device_ids") or []) if i in by_id] or list(by_id.values())
    mode, out = s.get("mode"), []
    for d in picked:
        modes = set(d.get("supported_modes") or [])
        if mode in ("charge", "discharge", "hold") and modes and mode not in modes:
            out.append(f"{_name(d)} cannot {mode} on command, so this schedule will not do anything to it.")
        elif mode == "charge" and d.get("power_control") is False and s.get("power_w"):
            out.append(f"{_name(d)} picks its own charge rate, so the power you set is ignored: it charges at its own speed and stops at the target.")
        tgt, floor = s.get("target_soc"), d.get("min_soc")
        if mode in ("charge", "discharge") and tgt is not None and floor is not None and tgt <= floor:
            out.append(f"The target ({tgt}%) is not above {_name(d)}'s minimum charge ({floor}%), so it would not charge past it.")
    return out


def goal_warnings(g: dict, devices: list[dict]) -> list[str]:
    by_id = {d["device_id"]: d for d in devices}
    picked = [by_id[i] for i in (g.get("device_ids") or []) if i in by_id] or list(by_id.values())
    out = []
    able = [d for d in picked if "charge" in set(d.get("supported_modes") or ["charge"])]
    if picked and not able:
        out.append("None of the batteries this goal covers can charge on command, so it cannot be met.")
    for d in able:
        floor = d.get("min_soc")
        if floor is not None and g.get("target_soc") is not None and g["target_soc"] <= floor and not g.get("auto_target"):
            out.append(f"The target ({g['target_soc']}%) is not above {_name(d)}'s minimum charge ({floor}%).")
    return out


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def window_drift(cfg_start: time | None, cfg_end: time | None, seen_start: time | None, seen_end: time | None,
                 tol_min: int = 60) -> str | None:
    """The cheap-rate window you typed in against the one the price history shows. A message if they now differ clearly."""
    if None in (cfg_start, cfg_end, seen_start, seen_end):
        return None

    def gap(a: time, b: time) -> int:
        d = abs(_minutes(a) - _minutes(b))
        return min(d, 1440 - d)

    if gap(cfg_start, seen_start) <= tol_min and gap(cfg_end, seen_end) <= tol_min:
        return None
    f = lambda t: t.strftime("%H:%M")  # noqa: E731
    return (f"Your prices look to have changed: the cheap rate now seems to run {f(seen_start)}-{f(seen_end)}, but HomePower is set "
            f"to {f(cfg_start)}-{f(cfg_end)}. Update the cheap-rate hours in Settings if that is right.")


def startup_summary(restored: dict[str, int], unreachable: list[str]) -> str:
    """One line after a restart: what HomePower picked up again, and which batteries it could not reach."""
    bits = [f"{n} {what}" for what, n in restored.items() if n]
    text = "Started. Restored " + (", ".join(bits) if bits else "nothing to restore (a fresh start)") + "."
    if unreachable:
        text += " Could not reach: " + ", ".join(unreachable) + "."
    return text


def hold_for_dearer(price_now: float | None, price_later_max: float | None, margin: float = 0.15) -> bool:
    """Is it worth keeping stored energy for later? Yes when a clearly dearer price is coming (the energy is already stored,
    so waiting costs nothing but the margin that stops it flapping between nearly equal prices)."""
    if price_now is None or price_later_max is None or price_later_max <= 0:
        return False
    return price_later_max > price_now * (1 + margin) and price_later_max - price_now > 0.01


def charge_efficiency(round_trip_pct: float | None, default: float = 0.92) -> float:
    """Efficiency of the charging half of a battery's round trip (the square root of the whole), kept within sensible bounds."""
    if not round_trip_pct or round_trip_pct <= 0:
        return default
    return min(0.99, max(0.75, sqrt(round_trip_pct / 100)))
