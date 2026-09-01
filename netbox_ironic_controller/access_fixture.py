from __future__ import annotations

import asyncio
import os

from .config import get_settings
from .sync import IronicSyncClient, NetBoxSyncClient


async def prepare_fixture(netbox, ironic, node_uuid: str, dcn_project_id: str,
                          profile: str, max_lease_days: int) -> None:
    expected = f"destroy:{node_uuid}"
    if os.environ.get("RACKD_ACCESS_FIXTURE_CONFIRM") != expected:
        raise RuntimeError(f"RACKD_ACCESS_FIXTURE_CONFIRM must equal {expected}")
    devices = await netbox.all("dcim/devices/", {"role": "server", "limit": 500})
    matches = [
        item for item in devices
        if (item.get("custom_fields") or {}).get("ironic_node_uuid") == node_uuid
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one NetBox device for fixture, got {len(matches)}")
    await ironic.prepare_access_fixture(node_uuid, dcn_project_id)
    device = matches[0]
    custom = dict(device.get("custom_fields") or {})
    custom.update({
        "baremetal_offer_enabled": True,
        "baremetal_profile": profile,
        "baremetal_max_lease_days": max_lease_days,
        "baremetal_lessee_project_id": "",
    })
    await netbox.patch("dcim/devices", device["id"], {"custom_fields": custom})


async def main() -> None:
    settings = get_settings()
    node_uuid = os.environ["RACKD_ACCESS_FIXTURE_NODE_UUID"]
    dcn_project_id = os.environ["RACKD_ACCESS_DCN_PROJECT_ID"]
    profile = os.environ["RACKD_ACCESS_FIXTURE_PROFILE"]
    max_lease_days = int(os.environ.get("RACKD_ACCESS_FIXTURE_MAX_LEASE_DAYS", "1"))
    if not 1 <= max_lease_days <= settings.access_max_lease_days:
        raise RuntimeError("fixture maximum lease days is outside the access policy")
    await prepare_fixture(
        NetBoxSyncClient(settings), IronicSyncClient(settings), node_uuid,
        dcn_project_id, profile, max_lease_days,
    )


if __name__ == "__main__":
    asyncio.run(main())
