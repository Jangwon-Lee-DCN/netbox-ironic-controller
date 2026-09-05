import re
from datetime import datetime, timezone


PORT_NUMBER = re.compile(r"(?:/|\s)(\d+)$")


def natural_port_key(name):
    match = PORT_NUMBER.search(name)
    return (0, int(match.group(1))) if match else (1, name.lower())


def normalize_observation(value, now=None, stale_seconds=180):
    now = now or datetime.now(timezone.utc)
    if not value:
        return {"oper_status": "unknown", "stale": True, "observed_at": None}
    observed = value.get("observed_at")
    try:
        observed_at = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        observed_at = None
    stale = observed_at is None or (now - observed_at).total_seconds() > stale_seconds
    state = value.get("oper_status") if value.get("oper_status") in {"up", "down", "unknown"} else "unknown"
    if stale:
        state = "unknown"
    return {**value, "oper_status": state, "stale": stale, "observed_at": observed}


def validate_observation(value):
    allowed = {"device", "interface", "oper_status", "admin_status", "speed_mbps",
               "duplex", "errors", "observed_at", "source"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("unsupported observation field")
    for required in ("device", "interface", "oper_status", "observed_at"):
        if not value.get(required):
            raise ValueError(f"missing {required}")
    if value["oper_status"] not in {"up", "down", "unknown"}:
        raise ValueError("invalid oper_status")
    normalize_observation(value, stale_seconds=10**9)
    return value
