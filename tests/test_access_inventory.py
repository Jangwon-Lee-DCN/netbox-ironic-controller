from netbox_ironic_controller.access_inventory import NetBoxIronicOfferInventory


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
