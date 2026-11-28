"""The control cycle's decide-and-send step when things change under it, on the real coordinator with simulated Anker batteries:
a battery removed or replaced while the cycle was reading is left alone; a battery handed back that reports its own control
again is confirmed; a protected battery is never overridden; Pause or shutdown arriving mid-cycle means nothing is sent
(except a hand-back); one battery's failing command is logged and the others' commands still go out."""
import _home  # noqa: F401 - sets up the paths first
import asyncio
from dataclasses import replace

from custom_components.homepower import coordinator as C
from custom_components.homepower.core.models import Desired, Mode
from custom_components.homepower.drivers.modbus import to_i32

real_compute = C.compute


def during_cycle(fn):
    """Run fn after every battery has been read and before anything is decided (the moment the real races happen)."""
    def compute(world):
        res = real_compute(world)
        fn()
        return res
    C.compute = compute


def before_sending(coord, fn):
    """Run fn once every battery has been decided, just before the commands go out (Pause pressed, or Home Assistant stopping,
    in that moment)."""
    real = type(coord)._send_lock

    def lock(self):
        fn()
        type(coord)._send_lock = real
        return real(self)
    type(coord)._send_lock = lock


def setpoint(fake):
    return to_i32(fake.hold[10071], fake.hold[10072], "big")


async def main():
    h = await _home.Home().start(batteries=("a", "b"))
    coord = h.coord
    fa, fb = h.fakes["a"], h.fakes["b"]
    charge = Desired(Mode.CHARGE, 900)
    try:
        # ---- removed while the cycle was reading: nothing is sent to it, and the other battery is still commanded
        coord.set_override(["a", "b"], charge, 30)
        during_cycle(lambda: coord._snap.devices.pop("a", None))
        await coord._async_update_data()
        C.compute = real_compute
        assert setpoint(fa) == 0 and setpoint(fb) == -900, (setpoint(fa), setpoint(fb))
        print("removed  : left alone; the other one commanded")
        await coord.release_all()
        await h.stop()

        # ---- replaced while reading (settings saved, so the adapter is a new one): its next cycle decides
        h = await _home.Home().start(batteries=("a",))
        coord, fa = h.coord, h.fakes["a"]
        coord.set_override(["a"], charge, 30)
        old = coord.adapters[0]

        def swap():
            coord.adapters = [type(old)(old.config, old.driver)]
        during_cycle(swap)
        await coord._async_update_data()
        C.compute = real_compute
        assert setpoint(fa) == 0, "decided for the old settings: not sent"
        await coord._async_update_data()
        assert setpoint(fa) == -900, "the next cycle decides with the new ones"
        print("replaced : waits for the next cycle")

        # ---- Pause while the cycle is reading: nothing is decided for the battery
        coord.set_override(["a"], Desired(Mode.CHARGE, 1500), 30)
        during_cycle(lambda: setattr(coord.manager, "paused", True))
        await coord._async_update_data()
        C.compute = real_compute
        st = coord._snap.devices["a"]
        assert setpoint(fa) == -900 and st.decision is None, (setpoint(fa), st.decision)
        coord.manager.paused = False
        # ---- Pause after the charge was decided, just before sending: dropped, not sent after the pause
        before_sending(coord, lambda: setattr(coord.manager, "paused", True))
        await coord._async_update_data()
        assert setpoint(fa) == -900 and st.decision is None, (setpoint(fa), st.decision)
        print("paused   : a command decided before the pause is not sent")
        coord.manager.paused = False

        # ---- shutting down mid-cycle: nothing new lands after the hand-back
        coord.set_override(["a"], Desired(Mode.CHARGE, 1700), 30)
        before_sending(coord, lambda: setattr(coord, "_closing", True))
        await coord._async_update_data()
        coord._closing = False
        assert setpoint(fa) == -900, setpoint(fa)
        print("closing  : nothing sent once shutdown began")

        # ---- a handed-back battery that reports its own control again is confirmed (no more hand-back retries)
        coord.clear_override(["a"])
        st = coord._snap.devices["a"]
        coord._set_release("a", st, True)
        coord.set_override(["a"], Desired(Mode.AUTO), 30)
        fa.hold[10064] = 0                                     # in its own mode, as after a hand-back
        await coord._async_update_data()
        assert st.release_pending is False and "a" not in coord._forced_ids, (st.release_pending, coord._forced_ids)
        print("released : confirmed once the battery reports its own control")
        coord.clear_override(["a"])

        # ---- protected (off-grid, calibrating): never overridden
        a = coord.adapters[0]
        real_read = a.read

        async def protected_read():
            return replace(await real_read(), protected=True)
        a.read = protected_read
        before = list(fa.writes)
        coord.set_override(["a"], Desired(Mode.CHARGE, 600), 30)
        await coord._async_update_data()
        st = coord._snap.devices["a"]
        assert fa.writes == before, "a protected battery was written to"
        events = " ".join(str(e) for e in coord._snap.events)
        assert "protection active" in events, events[-300:]
        print("protected: not overridden, and said so")
        a.read = real_read
        coord.clear_override(["a"])
        await coord.release_all()
        await h.stop()

        # ---- one battery's command fails: logged, and the other battery's command still goes out
        h = await _home.Home().start(batteries=("a", "b"))
        coord, fa, fb = h.coord, h.fakes["a"], h.fakes["b"]
        bad = next(x for x in coord.adapters if x.config.device_id == "a")

        async def boom(_desired):
            raise RuntimeError("the battery said no")
        bad.apply = boom
        coord.set_override(["a", "b"], charge, 30)
        await coord._async_update_data()                        # must not raise
        assert setpoint(fb) == -900 and setpoint(fa) == 0, (setpoint(fa), setpoint(fb))
        print("failure  : one battery's failure does not stop the other's command")
        bad.apply = type(bad).apply.__get__(bad)
        await coord.release_all()
    finally:
        C.compute = real_compute
        await h.stop()
    print("E2E CYCLE EDGES OK")


asyncio.run(main())
