from __future__ import annotations

from .access_domain import OfferCandidate
from .sync import IronicSyncClient, NetBoxSyncClient


class NetBoxIronicOfferInventory:
    def __init__(self, netbox: NetBoxSyncClient, ironic: IronicSyncClient):
        self.netbox = netbox
        self.ironic = ironic

    async def candidates(self, profile: str | None, rack: str | None = None) -> list[OfferCandidate]:
        devices = await self.netbox.all("dcim/devices/", {"role": "server", "limit": 500})
        nodes = await self.ironic.nodes()
        candidates = []
        for device in devices:
            custom = device.get("custom_fields") or {}
            node_uuid = custom.get("ironic_node_uuid")
            node = nodes.get(node_uuid)
            if not node or (profile is not None and custom.get("baremetal_profile") != profile):
                continue
            rack_name = ((device.get("rack") or {}).get("name") or "")
            if rack and rack_name != rack:
                continue
            status = device.get("status") or {}
            status_value = status.get("value") if isinstance(status, dict) else status
            candidates.append(OfferCandidate(
                node_uuid=node_uuid,
                rack=rack_name,
                profile=custom.get("baremetal_profile") or "",
                netbox_active=status_value == "active",
                offer_enabled=custom.get("baremetal_offer_enabled") is True,
                provision_state=node.get("provision_state") or "",
                maintenance=bool(node.get("maintenance")),
                lessee=node.get("lessee"),
                last_error=node.get("last_error"),
                max_lease_days=int(custom.get("baremetal_max_lease_days") or 30),
            ))
        return sorted(candidates, key=lambda item: (item.rack, item.node_uuid))


class IronicLeaseAdapter:
    def __init__(self, ironic: IronicSyncClient, netbox: NetBoxSyncClient,
                 dcn_project_id: str, deploy_image_ids: set[str] | None = None):
        self.ironic = ironic
        self.netbox = netbox
        self.dcn_project_id = dcn_project_id
        self.deploy_image_ids = deploy_image_ids or set()

    async def assign_lessee(self, node_uuid: str, project_id: str) -> None:
        await self.ironic.assign_lessee(node_uuid, project_id, self.dcn_project_id)
        await self._mirror_lessee(node_uuid, project_id)

    async def return_and_clean(self, node_uuid: str) -> None:
        await self.ironic.return_and_clean(node_uuid)

    async def clear_lessee(self, node_uuid: str) -> None:
        await self.ironic.clear_lessee(node_uuid)
        await self._mirror_lessee(node_uuid, "")

    async def _mirror_lessee(self, node_uuid: str, project_id: str) -> None:
        devices = await self.netbox.all("dcim/devices/", {"role": "server", "limit": 500})
        matches = [
            device for device in devices
            if (device.get("custom_fields") or {}).get("ironic_node_uuid") == node_uuid
        ]
        if len(matches) != 1:
            raise RuntimeError(f"expected one NetBox device for Ironic node {node_uuid}, got {len(matches)}")
        device = matches[0]
        custom = dict(device.get("custom_fields") or {})
        custom["baremetal_lessee_project_id"] = project_id
        await self.netbox.patch("dcim/devices", device["id"], {"custom_fields": custom})

    async def deploy(self, node_uuid: str, project_id: str, image_id: str,
                     config_drive: dict) -> None:
        if image_id not in self.deploy_image_ids:
            raise RuntimeError("image is not approved for bare metal deployment")
        await self.ironic.deploy(node_uuid, project_id, image_id, config_drive, self.dcn_project_id)

    async def set_power(self, node_uuid: str, project_id: str, action: str) -> None:
        await self.ironic.set_power(node_uuid, project_id, action, self.dcn_project_id)

    async def approved_images(self) -> list[dict]:
        return await self.ironic.approved_images(self.deploy_image_ids)
