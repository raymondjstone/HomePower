"""EcoFlow STREAM AC 5000 over the app protocol: protobuf codec, frame decoding, commands, and the driver's Hold/Auto."""
import asyncio
import struct

import pytest

from core.models import Capabilities, Desired, DeviceConfig, Mode
from core.serde import ValidationError, current_abilities, device_from_dict, device_to_dict
from hp.drivers import pbwire as pb
from hp.drivers.ecoflow_ac5000 import (EcoFlowAc5000Driver, actual_from, build_set_command, decode_frame, describe_frame,
                                       out_limit_pdata)


def f32(field, x):
    return pb.enc_varint(field << 3 | 5) + struct.pack("<f", x)


def frame(cmd_id, pdata, sn="ES22TEST"):
    header = pb.enc_bytes(1, pdata) + pb.enc_int(8, 254) + pb.enc_int(9, cmd_id) + pb.enc_bytes(25, sn)
    return pb.enc_bytes(1, header)


def test_varints_and_fields_roundtrip():
    for n in (0, 1, 127, 128, 300, 2**32, 2**63):
        assert pb._varint(pb.enc_varint(n), 0)[0] == n
    m = pb.parse(pb.enc_int(1, 7) + pb.enc_bytes(2, "hi") + f32(3, 2.5) + pb.enc_bytes(4, pb.enc_int(1, 9)))
    assert pb.num(m, 1) == 7 and pb.text(m, 2) == "hi" and pb.num(m, 3) == 2.5 and pb.num(pb.sub(m, 4), 1) == 9
    assert pb.parse(b"\xff\xff\xff") == {} and pb.parse(b"\x0a\x09abc") == {}     # damaged: no exception


def test_runtime_frame_decodes_to_readings():
    meter = pb.enc_bytes(3, "ES41X") + f32(4, 100.0) + f32(16, 250.0)
    cfg = f32(5, 4800.0) + f32(8, 5.0) + f32(10, 800.0)
    cms = pb.enc_int(1, 62) + pb.enc_int(2, 1800) + pb.enc_int(7, 90)            # 1800 half-watts = 900 W
    stat = pb.enc_bytes(1, pb.enc_bytes(1, "ES22TEST") + pb.enc_int(2, 64))
    port = pb.enc_bytes(1, f32(4, 905.0))
    limits = pb.enc_int(1, 800) + pb.enc_int(2, 3000)
    payload = (pb.enc_bytes(16, meter) + pb.enc_bytes(33, cfg) + pb.enc_bytes(44, cms) + pb.enc_bytes(54, stat)
               + pb.enc_bytes(50, port) + pb.enc_bytes(10, limits) + pb.enc_int(25, 0))
    q = decode_frame(frame(39, payload) + frame(2, pb.enc_bytes(1, pb.enc_int(7, 100) + f32(15, 61.5))))
    assert q["soc"] == 64 and q["gridPortPower"] == 905.0 and q["bpPower"] == 900.0
    assert q["meterTotalPower"] == 250.0 and q["meterLinkUp"] == 1 and q["cmsBattFullEnergy"] == 4800.0
    assert q["feedGridModePowLimit"] == 800 and q["chgPowLimit"] == 3000 and q["workMode"] == 0 and q["packSoc"] == 61.5
    a = actual_from(q)
    assert (a.soc, a.power_w, a.grid_w, a.mode) == (64.0, 905.0, 250.0, Mode.AUTO)
    # An empty meter block means the P1 meter dropped out: its stale figure is not used.
    q2 = {**q, **decode_frame(frame(39, pb.enc_bytes(16, b"")))}
    assert q2["meterLinkUp"] == 0 and actual_from(q2).grid_w is None
    assert actual_from({**q, "feedGridModePowLimit": 0}).mode is Mode.HOLD
    assert decode_frame(b"garbage") == {} and decode_frame(frame(99, b"\x08\x01")) == {}


def test_set_command_envelope():
    pdata = out_limit_pdata(0)
    top = pb.parse(build_set_command("ES22TEST", pdata, seq=1234))
    h = pb.parse(top[1][0])
    assert h[1][0] == pdata and pb.num(h, 8) == 254 and pb.num(h, 9) == 38 and pb.num(h, 14) == 1234 and pb.num(h, 10) == len(pdata)
    assert pb.text(h, 25) == pb.text(h, 26) == pb.text(h, 27) == "ES22TEST" and pb.num(h, 16) == 4 and pb.num(h, 11) == 1
    cmd = pb.parse(pdata)
    assert pb.num(cmd, 1) == 10 and pb.num(pb.sub(cmd, 10), 1) == 0 and pb.num(pb.sub(cmd, 10), 5) == 800
    assert "cmd_id=38" in describe_frame("/app/1/ES22TEST/thing/property/set", build_set_command("ES22TEST", pdata))


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0):
        self.sent.append((topic, payload))
        return type("I", (), {"rc": 0})()


def ac5000(state):
    c = DeviceConfig("e", "E", "ecoflow_stream_ac_5000", Capabilities(5000, 3000, 0, 10),
                     extra={"connection": {"sn": "ES22TEST", "email": "a@b", "password": "p", "region": "eu"}})
    d = EcoFlowAc5000Driver(c, session=None)
    d._state, d._uid, d.connected, d._client = dict(state), "42", True, FakeClient()
    d._ensure = lambda: None
    return d


def test_hold_remembers_the_limit_and_auto_restores_it():
    async def go():
        d = ac5000({"feedGridModePowLimit": 800, "feedGridModePowMax": 2500})
        await d.apply(Desired(Mode.HOLD))
        topic, payload = d._client.sent[-1]
        assert topic == "/app/42/ES22TEST/thing/property/set"
        cmd = pb.parse(pb.parse(pb.parse(payload)[1][0])[1][0])
        assert pb.num(pb.sub(cmd, 10), 1) == 0
        await d.apply(Desired(Mode.HOLD))                    # held again: still remembers 800, not 0
        await d.apply(Desired(Mode.AUTO))
        cmd = pb.parse(pb.parse(pb.parse(d._client.sent[-1][1])[1][0])[1][0])
        assert pb.num(pb.sub(cmd, 10), 1) == 800 and len(d._client.sent) == 3
        await d.apply(Desired(Mode.AUTO))                    # nothing held: nothing sent
        assert len(d._client.sent) == 3
        with pytest.raises(ValueError):
            await d.apply(Desired(Mode.CHARGE, 1000))
        e = ac5000({})
        with pytest.raises(ValueError):
            await e.apply(Desired(Mode.HOLD))                # limit unknown: refuse rather than guess
        f = ac5000({"feedGridModePowLimit": 0, "feedGridModePowMax": 2500})   # held before a restart
        await f.apply(Desired(Mode.AUTO))
        cmd = pb.parse(pb.parse(pb.parse(f._client.sent[-1][1])[1][0])[1][0])
        assert pb.num(pb.sub(cmd, 10), 1) == 2500
    asyncio.run(go())


def test_driver_reads_pushed_frames_and_captures_app_commands():
    async def go():
        d = ac5000({})
        assert (await d.read()).online is False
        d._dispatch(d._data_topic, frame(39, pb.enc_bytes(54, pb.enc_bytes(1, pb.enc_int(2, 55)))))
        assert (await d.read()).soc == 55
        d._dispatch("/app/42/ES22TEST/thing/property/set", build_set_command("ES22TEST", pb.enc_int(1, 25) + pb.enc_int(25, 2)))
        assert any(k.startswith("app_command_") and "cmd_id=38" in v for k, v in d.last_quota.items())
        assert d.last_quota["soc"] == 55
    asyncio.run(go())


CONN = {"region": "eu", "email": "me@example.com", "password": "pw", "sn": "ES22TEST"}
BASE = {"device_id": "es", "name": "ES", "kind": "ecoflow_stream_ac_5000", "capacity_wh": 5000, "max_charge_w": 0, "max_discharge_w": 0}


def test_ac5000_profile_validates_its_login_and_offers_auto_and_hold():
    c = device_from_dict({**BASE, "connection": CONN})
    assert c.extra["driver"] == "ecoflow_ac5000" and c.caps.supported_modes == {Mode.AUTO, Mode.HOLD}
    assert not c.caps.power_control and c.caps.max_charge_w == 3000
    for missing in ("email", "password", "sn"):
        with pytest.raises(ValidationError):
            device_from_dict({**BASE, "connection": {**CONN, missing: ""}})


def test_saved_es5000_with_only_developer_keys_keeps_reading():
    old = {**BASE, "connection": {"region": "eu", "access_key": "a", "secret_key": "s", "sn": "ES22TEST"}}
    c = device_from_dict(old)
    assert c.extra["driver"] == "ecoflow_stream" and c.caps.supported_modes == {Mode.AUTO}
    assert current_abilities(old)["supported_modes"] == ["auto"]
    assert device_to_dict(c)["driver"] == "ecoflow_stream"
