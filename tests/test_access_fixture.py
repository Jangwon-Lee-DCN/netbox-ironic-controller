from types import SimpleNamespace

import pytest

from netbox_ironic_controller.access_fixture import prepare_fixture


NODE = "11111111-1111-1111-1111-111111111111"


class NetBox:
    def __init__(self):
        self.patch_value = None

    async def all(self, path, params):
        if path == "dcim/racks/":
            return [{"id": 3, "name": "Rack-01"}]
        return [{"id": 7, "custom_fields": {"ironic_node_uuid": NODE, "keep": "yes"}}]

    async def patch(self, path, object_id, payload):
        self.patch_value = (path, object_id, payload)


class Ironic:
    def __init__(self): self.calls = []

    async def prepare_access_fixture(self, node_uuid, project_id):
        self.calls.append((node_uuid, project_id))


async def test_fixture_requires_exact_destructive_confirmation(monkeypatch):
    monkeypatch.setenv("RACKD_ACCESS_FIXTURE_CONFIRM", "wrong")
    with pytest.raises(RuntimeError, match="must equal destroy"):
        await prepare_fixture(NetBox(), Ironic(), NODE, "dcn", "e2e", "Rack-01", 1)


async def test_fixture_updates_only_offer_fields_after_ironic_ready(monkeypatch):
    monkeypatch.setenv("RACKD_ACCESS_FIXTURE_CONFIRM", f"destroy:{NODE}")
    netbox, ironic = NetBox(), Ironic()
    await prepare_fixture(netbox, ironic, NODE, "dcn", "e2e", "Rack-01", 1)
    assert ironic.calls == [(NODE, "dcn")]
    assert netbox.patch_value[0:2] == ("dcim/devices", 7)
    assert netbox.patch_value[2]["status"] == "active"
    assert netbox.patch_value[2]["rack"] == 3
    assert netbox.patch_value[2]["custom_fields"] == {
        "ironic_node_uuid": NODE, "keep": "yes", "baremetal_offer_enabled": True,
        "baremetal_profile": "e2e", "baremetal_max_lease_days": 1,
        "baremetal_lessee_project_id": "",
    }


async def test_fixture_rejects_missing_exact_rack(monkeypatch):
    monkeypatch.setenv("RACKD_ACCESS_FIXTURE_CONFIRM", f"destroy:{NODE}")
    netbox = NetBox()
    original_all = netbox.all

    async def all_without_rack(path, params):
        if path == "dcim/racks/":
            return []
        return await original_all(path, params)

    netbox.all = all_without_rack
    with pytest.raises(RuntimeError, match="expected one NetBox rack"):
        await prepare_fixture(netbox, Ironic(), NODE, "dcn", "e2e", "Rack-01", 1)
