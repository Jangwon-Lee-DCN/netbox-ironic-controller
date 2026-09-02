import pytest

from netbox_ironic_controller.access_inventory import IronicLeaseAdapter, NetBoxIronicOfferInventory


class NetBox:
    async def all(self, path, params=None):
        return [{
            "status": {"value": "active"}, "rack": {"name": "Rack 1"},
            "custom_fields": {"ironic_node_uuid": "node-1", "baremetal_profile": "general",
                              "baremetal_offer_enabled": True},
        }]


class Ironic:
    async def nodes(self):
        return {"node-1": {"provision_state": "available", "maintenance": False,
                           "lessee": None, "last_error": ""}}


async def test_inventory_requires_matching_netbox_and_ironic_state():
    rows = await NetBoxIronicOfferInventory(NetBox(), Ironic()).candidates("general", "Rack 1")
    assert len(rows) == 1
    assert rows[0].eligible


async def test_inventory_does_not_expose_a_different_profile_or_rack():
    inventory = NetBoxIronicOfferInventory(NetBox(), Ironic())
    assert await inventory.candidates("gpu", "Rack 1") == []
    assert await inventory.candidates("general", "Rack 2") == []


class LeaseIronic:
    def __init__(self):
        self.calls = []

    async def assign_lessee(self, node, project, owner):
        self.calls.append(("assign", node, project, owner))

    async def clear_lessee(self, node):
        self.calls.append(("clear", node))


class LeaseNetBox:
    def __init__(self):
        self.patches = []

    async def all(self, path, params):
        return [{"id": 7, "custom_fields": {"ironic_node_uuid": "node-1", "keep": "value"}}]

    async def patch(self, path, object_id, payload):
        self.patches.append((path, object_id, payload))


async def test_lease_adapter_mirrors_runtime_lessee_without_dropping_fields():
    ironic, netbox = LeaseIronic(), LeaseNetBox()
    adapter = IronicLeaseAdapter(ironic, netbox, "dcn")
    await adapter.assign_lessee("node-1", "tenant-a")
    await adapter.clear_lessee("node-1")
    assert ironic.calls == [("assign", "node-1", "tenant-a", "dcn"), ("clear", "node-1")]
    assert netbox.patches[0][2]["custom_fields"] == {
        "ironic_node_uuid": "node-1", "keep": "value", "baremetal_lessee_project_id": "tenant-a",
    }
    assert netbox.patches[1][2]["custom_fields"]["baremetal_lessee_project_id"] == ""


def test_lease_adapter_rejects_malformed_manual_clean_steps():
    with pytest.raises(ValueError, match="interface is invalid"):
        IronicLeaseAdapter(LeaseIronic(), LeaseNetBox(), "dcn", clean_steps=[{"step": "erase"}])


async def test_lease_adapter_returns_and_deploys_contract_pinned_images():
    class ImageIronic(LeaseIronic):
        async def deploy(self, *args):
            self.calls.append(("deploy", args))

    image = {"id": "image-1", "name": "Ubuntu", "checksum": "abc", "disk_format": "qcow2",
             "source_url": "https://images.example/ubuntu.img", "source_checksum": "a" * 64}
    ironic = ImageIronic()
    adapter = IronicLeaseAdapter(ironic, LeaseNetBox(), "dcn", deploy_images=[image])
    assert await adapter.approved_images() == [{"id": "image-1", "name": "Ubuntu"}]
    await adapter.deploy("node-1", "tenant-a", "image-1", {})
    assert ironic.calls == [("deploy", ("node-1", "tenant-a", "image-1", {}, "dcn", image))]


def test_lease_adapter_rejects_incomplete_contract_image_metadata():
    with pytest.raises(ValueError, match="metadata is incomplete"):
        IronicLeaseAdapter(LeaseIronic(), LeaseNetBox(), "dcn", deploy_images=[{"id": "image-1"}])
