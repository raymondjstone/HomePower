"""Batteries found on the network, matched against the ones HomePower has (core/found.py)."""
from hp.core.found import anker_kinds, discovered_name, known_hosts, match_known, norm_serial
from hp.core.serde import device_from_dict, device_to_dict


def _anker(did="a", host="192.168.1.20", port=502, serial=None):
    conn = {"host": host, "port": port, **({"serial": serial} if serial else {})}
    return {"device_id": did, "name": did, "kind": "anker_e5000_pro", "driver": "anker_modbus", "connection": conn}


def _ecoflow_cloud(did="e", sn="ES21ABC123"):
    return {"device_id": did, "name": did, "kind": "ecoflow_stream_ultra", "driver": "ecoflow_stream", "connection": {"sn": sn, "region": "eu"}}


def test_new_battery_is_not_known():
    assert match_known({"a": _anker(serial="AAA")}, "anker", "192.168.1.30", 502, "BBB") == (None, None)
    assert match_known({}, "anker", "192.168.1.30", 502, None) == (None, None)


def test_same_battery_at_a_new_address_gets_the_new_address():
    did, update = match_known({"a": _anker(serial="AAA")}, "anker", "192.168.1.31", 502, "aaa ")
    assert did == "a" and update["connection"] == {"host": "192.168.1.31", "port": 502, "serial": "AAA"}


def test_same_battery_at_the_same_address_needs_nothing():
    assert match_known({"a": _anker(serial="AAA")}, "anker", "192.168.1.20", 502, "AAA") == ("a", None)


def test_a_battery_saved_without_its_serial_gets_it():
    did, update = match_known({"a": _anker()}, "anker", "192.168.1.20", 502, "AAA")
    assert did == "a" and update["connection"]["serial"] == "AAA" and update["connection"]["host"] == "192.168.1.20"


def test_another_battery_now_at_a_known_address_is_new():
    # the battery that had this address moved away, and a different one (another serial) got it
    assert match_known({"a": _anker(serial="AAA")}, "anker", "192.168.1.20", 502, "BBB") == (None, None)


def test_no_serial_matches_by_address_only():
    assert match_known({"a": _anker(serial="AAA")}, "anker", "192.168.1.20", 502, None) == ("a", None)
    assert match_known({"a": _anker(serial="AAA")}, "anker", "192.168.1.21", 502, None) == (None, None)


def test_vendors_do_not_match_each_other():
    assert match_known({"a": _anker(serial="AAA")}, "ecoflow", "192.168.1.20", 502, "AAA") == (None, None)


def test_an_ecoflow_battery_added_through_the_cloud_is_known_by_its_serial():
    # the same STREAM announcing itself on the network: no second copy is offered, and nothing is rewritten (it has no address)
    assert match_known({"e": _ecoflow_cloud()}, "ecoflow", "192.168.1.40", 502, "es21abc123") == ("e", None)


def test_known_hosts_are_the_local_batteries_only():
    assert known_hosts({"a": _anker(), "e": _ecoflow_cloud()}) == {"192.168.1.20"}


def test_the_serial_is_kept_through_validation():
    d = device_to_dict(device_from_dict({**_anker(serial="AAA"), "capacity_wh": 5000, "max_charge_w": 2000, "max_discharge_w": 2000}))
    assert d["connection"]["serial"] == "AAA"
    d = device_to_dict(device_from_dict({**_anker(), "capacity_wh": 5000, "max_charge_w": 2000, "max_discharge_w": 2000}))
    assert "serial" not in d["connection"]


def test_anker_model_choice_and_names():
    kinds, start = anker_kinds(True)
    assert start == "anker_x1" and {k["value"] for k in kinds} >= {"anker_x1", "anker_e5000_pro"}
    assert anker_kinds(False)[1] is None           # the Solarbank models cannot be told apart from the scan: the user picks
    assert discovered_name("ecoflow", "STREAM AC 5000", "ES21ABC123") == "STREAM AC 5000 C123"
    assert discovered_name("anker", None, None) == "Anker battery"
    assert norm_serial("  ") is None and norm_serial(" ab1 ") == "AB1"


def test_new_device_id_never_overwrites_and_is_always_valid():
    from hp.core.found import new_device_id
    assert new_device_id("Garage Battery", {}) == "garage_battery"
    assert new_device_id("Garage Battery", {"garage_battery": {}, "garage_battery_2": {}}) == "garage_battery_3"
    assert new_device_id("  ***  ", {}) == "battery"            # nothing usable in the name
    assert new_device_id(None, {"battery": {}}) == "battery_2"
    assert len(new_device_id("x" * 200, {})) <= 96               # the id limit save_device checks
