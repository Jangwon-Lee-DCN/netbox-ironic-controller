from datetime import datetime, timedelta, timezone

import pytest

from netbox_dcn_port_panel.panel import natural_port_key, normalize_observation, validate_observation


def test_natural_port_order():
    ports = ["fortyGigE 1/50", "TenGigabitEthernet 1/2", "TenGigabitEthernet 1/11"]
    assert sorted(ports, key=natural_port_key) == ["TenGigabitEthernet 1/2", "TenGigabitEthernet 1/11", "fortyGigE 1/50"]


def test_missing_observation_is_unknown():
    assert normalize_observation(None)["oper_status"] == "unknown"


def test_fresh_observation_is_preserved():
    now = datetime.now(timezone.utc)
    value = {"oper_status": "up", "observed_at": now.isoformat(), "speed_mbps": 10000}
    result = normalize_observation(value, now=now)
    assert result["oper_status"] == "up"
    assert result["stale"] is False


def test_stale_observation_fails_closed_to_unknown():
    now = datetime.now(timezone.utc)
    value = {"oper_status": "up", "observed_at": (now - timedelta(minutes=10)).isoformat()}
    result = normalize_observation(value, now=now, stale_seconds=180)
    assert result["oper_status"] == "unknown"
    assert result["stale"] is True


def test_observation_contract_rejects_unknown_fields_and_states():
    with pytest.raises(ValueError):
        validate_observation({"device": "tor-r1", "interface": "Te1/1", "oper_status": "active", "observed_at": "2026-09-04T00:00:00Z"})
    with pytest.raises(ValueError):
        validate_observation({"device": "tor-r1", "interface": "Te1/1", "oper_status": "up", "observed_at": "2026-09-04T00:00:00Z", "password": "never"})
