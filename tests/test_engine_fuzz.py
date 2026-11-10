"""Randomised worlds: whatever the inputs, the engine must never break a safety invariant."""
import random
from datetime import datetime, time, timedelta, timezone

from core.engine import DevIn, Settings, World, compute
from core.goals import Goal
from core.models import Actual, Capabilities, Desired, DeviceConfig, Mode
from core.rates import RateTable
from core.schedule import ScheduleEntry

UTC = timezone.utc


def rand_world(rng: random.Random) -> World:
    now = datetime(2026, 11, 10, 0, 0, tzinfo=UTC) + timedelta(minutes=rng.randrange(0, 7 * 24 * 60, 5))
    devs = []
    for i in range(rng.randint(1, 4)):
        modes = frozenset(Mode) if rng.random() < 0.7 else frozenset({Mode.AUTO, Mode.HOLD})
        caps = Capabilities(rng.choice([2000, 5000, 10000]), rng.choice([0, 1000, 3000, 5000]),
                            rng.choice([0, 1000, 3000, 5000]), rng.choice([0, 10, 20]), modes)
        online = rng.random() > 0.15
        soc = rng.choice([None, 0, 5, 10, 12, 30, 50, 79, 80, 95, 100]) if online else None
        act = Actual(online=online, soc=soc, power_w=rng.uniform(-3000, 3000), mode=rng.choice(list(Mode)),
                     set_power_w=rng.choice([None, 0, 500, 2000]), grid_w=None)
        devs.append(DevIn(DeviceConfig(f"d{i}", f"d{i}", "x", caps, priority=rng.randint(-2, 2), extra={}), act,
                          release_pending=rng.random() < 0.2, guard_held=rng.random() < 0.3,
                          controllable=rng.random() > 0.2))
    ids = [d.config.device_id for d in devs]
    scheds = []
    for k in range(rng.randint(0, 3)):
        start = time(rng.randrange(24), rng.choice([0, 30]))
        end = time(rng.randrange(24), rng.choice([0, 30]))
        if start == end:
            continue
        mode = rng.choice(list(Mode))
        scheds.append(ScheduleEntry(f"s{k}", frozenset(rng.sample(ids, rng.randint(0, len(ids)))), start, end,
                                    Desired(mode, rng.choice([None, 500, 2000, 9000]), target_soc=rng.choice([None, 60, 100]),
                                            reserve_soc=rng.choice([None, 30])),
                                    priority=rng.randint(0, 2), total_power_w=rng.choice([None, None, 4000]),
                                    presence=rng.choice(["any", "any", "home", "away"])))
    goals = [Goal(f"g{k}", frozenset(), rng.choice([60, 90, 100]), time(rng.randrange(24)), use_forecast=rng.random() < 0.5)
             for k in range(rng.randint(0, 2))]
    st = Settings(charge_cap_w=rng.choice([None, 2000, 6000]), discharge_cap_w=rng.choice([None, 2500]),
                  import_limit_w=rng.choice([None, 7000, 3000]), export_limit_w=rng.choice([None, 2000]),
                  zero_import_device=rng.choice([None] + ids), peak_shaving=rng.random() < 0.5,
                  backup_pct=rng.choice([30, 50]))
    return World(
        now=now, devices=devs, settings=st, schedules=scheds, goals=goals,
        overrides={rng.choice(ids): Desired(rng.choice(list(Mode)), rng.choice([None, 800]))} if rng.random() < 0.15 else {},
        grid_w=rng.choice([None, -6000, -1500, 0, 800, 4000, 9000]), backup_active=rng.random() < 0.2, away=rng.random() < 0.3,
        load_blocks=set(rng.sample(["charge", "discharge"], rng.randint(0, 2))),
        rates=rng.choice([None, RateTable(peak=30.0, offpeak=7.5, offpeak_start=time(23, 30), offpeak_end=time(5, 30))]),
        forecast_kwh=rng.choice([None, 4.0, 20.0]),
    )


def test_engine_invariants_hold_for_random_worlds():
    rng = random.Random(1234)
    for n in range(4000):
        w = rand_world(rng)
        r = compute(w)
        by = {d.config.device_id: d for d in w.devices}
        ctx = f"world #{n}"
        tot_c = tot_d = 0
        for did, want in r.wants.items():
            d, caps = by[did], by[did].config.caps
            assert did in by, ctx
            assert want.mode in caps.supported_modes, f"{ctx}: {did} given unsupported {want.mode}"
            if want.mode is Mode.CHARGE:
                assert want.power_w is None or 0 <= want.power_w <= caps.max_charge_w, f"{ctx}: charge power {want.power_w} > {caps.max_charge_w}"
                tot_c += want.power_w or 0
            if want.mode is Mode.DISCHARGE:
                assert want.power_w is None or 0 <= want.power_w <= caps.max_discharge_w, f"{ctx}: discharge power"
                tot_d += want.power_w or 0
                reason = r.reasons[did]
                if d.actual.online and d.actual.soc is not None and reason != "manual override":
                    floor = max(caps.min_soc, w.settings.backup_pct) if w.backup_active else caps.min_soc
                    assert d.actual.soc > floor, f"{ctx}: discharge at soc {d.actual.soc} <= floor {floor} ({reason})"
                if "discharge" in w.load_blocks:
                    assert reason in ("manual override", "backup reserve"), f"{ctx}: discharge despite load rule ({reason})"
            if want.mode is Mode.CHARGE and "charge" in w.load_blocks:
                assert r.reasons[did] in ("manual override", "backup reserve"), f"{ctx}: charge despite load rule"
            if want.mode is not Mode.AUTO and r.reasons[did] in ("peak shaving", "export limit"):
                assert d.controllable and d.actual.online, f"{ctx}: guard used an unusable battery"
            assert r.reasons.get(did), f"{ctx}: no reason for {did}"
        if w.settings.discharge_cap_w is not None:
            assert tot_d <= w.settings.discharge_cap_w + 1, f"{ctx}: discharge total {tot_d} over cap"
        # every battery we forced earlier and nothing instructs gets handed back
        for d in w.devices:
            if d.release_pending and d.config.device_id not in r.wants:
                raise AssertionError(f"{ctx}: forced battery {d.config.device_id} neither instructed nor released")
        # guard_held is only reported for instructed batteries
        assert set(r.guard_held) <= set(r.wants) | set(by)


def test_guards_only_worlds_and_charge_ceiling():
    from core.limits import charge_ceiling
    rng = random.Random(99)
    fired = {"peak shaving": 0, "export limit": 0}
    for n in range(12000):
        w = rand_world(rng)
        w.schedules, w.goals, w.overrides, w.backup_active = [], [], {}, False
        w.settings.zero_import_device = None
        r = compute(w)
        by = {d.config.device_id: d for d in w.devices}
        S = w.settings
        online_net = sum(d.actual.power_w or 0.0 for d in w.devices if d.actual.online)
        ceiling = charge_ceiling(S.charge_cap_w, S.import_limit_w, w.grid_w, online_net)
        tot_c = sum((x.power_w or 0) for x in r.wants.values() if x.mode is Mode.CHARGE)
        if ceiling is not None:
            assert tot_c <= ceiling + 1, f"world {n}: total charge {tot_c} over ceiling {ceiling}"
        for did, want in r.wants.items():
            why = r.reasons[did]
            if why == "peak shaving":
                fired[why] += 1
                assert S.peak_shaving and S.import_limit_w and w.grid_w is not None
                cur = sum(max(0.0, -(by[x].actual.power_w or 0.0)) for x in by)
                assert w.grid_w + cur > S.import_limit_w * 0.95 - 1, f"world {n}: shaving while under the limit"
                assert want.mode is Mode.DISCHARGE or (want.mode is Mode.HOLD and S.discharge_cap_w is not None), f"world {n}: {want}"
            if why == "export limit":
                fired[why] += 1
                assert S.export_limit_w and w.grid_w is not None and -w.grid_w > S.export_limit_w * 0.95 - 1
                assert want.mode is Mode.CHARGE or (want.mode is Mode.HOLD and ceiling is not None), f"world {n}: {want}"   # HOLD = a cap cut it to zero
                assert by[did].actual.soc < 98
    assert fired["peak shaving"] > 100 and fired["export limit"] > 100, fired
