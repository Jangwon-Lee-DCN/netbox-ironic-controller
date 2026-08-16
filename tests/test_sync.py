from netbox_ironic_controller.config import Settings
from netbox_ironic_controller.sync import NetBoxIronicController


class FakeNetBox:
    def __init__(self, devices):
        self.devices = devices
        self.patches = []
        self.created = []

    async def all(self, path, params=None):
        if path == "dcim/devices/":
            if params and params.get("name"):
                return [d for d in self.devices if d["name"] == params["name"]]
            return self.devices
        if path == "dcim/interfaces/":
            return [{"primary_mac_address": {"address": "52:54:00:12:34:56"}}]
        if path == "dcim/device-types/":
            return [{"id": 10}]
        if path == "dcim/sites/":
            return [{"id": 20}]
        if path == "dcim/device-roles/":
            return [{"id": 30}]
        return []

    async def patch(self, path, object_id, payload):
        self.patches.append((path, object_id, payload))
        return payload

    async def create(self, path, payload):
        self.created.append((path, payload))
        return {"id": 99, **payload}


class FakeIronic:
    def __init__(self, nodes=None):
        self._nodes = nodes or {}
        self.created = []
        self.created_ports = []

    async def nodes(self):
        return self._nodes

    async def create_node(self, **kwargs):
        self.created.append(kwargs)
        return "new-node-uuid"

    async def port_addresses(self, node_uuid):
        return set()

    async def create_port(self, node_uuid, address):
        self.created_ports.append((node_uuid, address))


class FakeSecrets:
    async def credentials(self, name):
        assert name == "server-01-bmc"
        return "operator", "secret"


async def test_netbox_managed_server_is_created_in_ironic():
    device = {"id": 1, "name": "server-01", "custom_fields": {
        "ironic_managed": True, "bmc_address": "https://192.0.2.10",
        "bmc_secret_name": "server-01-bmc", "ironic_driver": "redfish",
        "ironic_resource_class": "baremetal", "ironic_properties": '{"cpus": 16}',
    }}
    netbox, ironic = FakeNetBox([device]), FakeIronic()
    result = await NetBoxIronicController(Settings(), netbox, ironic, FakeSecrets()).reconcile()
    assert result.created_in_ironic == 1
    assert ironic.created[0]["properties"] == {"cpus": 16}
    assert netbox.patches[0][2]["custom_fields"]["ironic_node_uuid"] == "new-node-uuid"
    assert ironic.created_ports == [("new-node-uuid", "52:54:00:12:34:56")]


async def test_ironic_only_node_is_discovered_in_netbox():
    node = {"id": "abcdef123456", "name": "found-node", "driver": "ipmi",
            "power_state": "power off", "provision_state": "manageable",
            "maintenance": False, "last_error": None}
    netbox, ironic = FakeNetBox([]), FakeIronic({node["id"]: node})
    result = await NetBoxIronicController(Settings(), netbox, ironic, FakeSecrets()).reconcile()
    assert result.discovered_in_netbox == 1
    assert netbox.created[0][1]["status"] == "planned"
    assert netbox.created[0][1]["custom_fields"]["ironic_node_uuid"] == node["id"]
