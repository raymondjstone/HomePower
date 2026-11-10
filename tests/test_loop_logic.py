"""HA-free simulation of the policy the coordinator applies each cycle."""
from datetime import datetime, time

from core.limits import soc_guard
from core.models import Actual, Capabilities, Desired, Mode
from core.reconcile import Decision, decide
from core.schedule import ScheduleEntry, resolve

CAPS = Capabilities(5000, 3000, 3000, 10)
NIGHT = ScheduleEntry("n", frozenset(), time(1, 0), time(2, 0), Desired(Mode.CHARGE, 2000, target_soc=80))


def cycle(now, soc, release_pending, guard_held, actual_mode):
    want = resolve([NIGHT], "d", now)
    if want is None and release_pending:
        want = Desired(Mode.AUTO)  # coordinator hands the battery back
    if want is None:
        return None, release_pending, False
    g = soc_guard(want, soc, CAPS, held=guard_held)
    held = g.mode is not want.mode
    if g.mode is not Mode.AUTO:
        release_pending = True
    dec, _ = decide(g, Actual(True, soc=soc, mode=actual_mode, set_power_w=2000 if actual_mode is Mode.CHARGE else None),
                    now=1000.0, last_apply=None)
    if g.mode is Mode.AUTO and actual_mode is Mode.AUTO:
        release_pending = False
    return g, release_pending, held


def test_released_after_window_and_then_left_alone():
    g, rel, _ = cycle(datetime(2026, 11, 10, 1, 30), 50, False, False, Mode.AUTO)
    assert g.mode is Mode.CHARGE and rel is True
    g, rel, _ = cycle(datetime(2026, 11, 10, 2, 30), 60, rel, False, Mode.CHARGE)   # window over
    assert g.mode is Mode.AUTO and rel is True                                      # handing back
    g, rel, _ = cycle(datetime(2026, 11, 10, 2, 31), 60, rel, False, Mode.AUTO)     # confirmed
    assert g.mode is Mode.AUTO and rel is False
    g, rel, _ = cycle(datetime(2026, 11, 10, 2, 32), 60, rel, False, Mode.AUTO)
    assert g is None                                                                # unmanaged again


def test_soc_target_holds_then_resumes_with_margin():
    t = datetime(2026, 11, 10, 1, 30)
    g, rel, held = cycle(t, 80, False, False, Mode.CHARGE)
    assert g.mode is Mode.HOLD and held
    g, rel, held = cycle(t, 79, rel, held, Mode.HOLD)
    assert g.mode is Mode.HOLD
    g, rel, held = cycle(t, 77, rel, held, Mode.HOLD)
    assert g.mode is Mode.CHARGE and not held
