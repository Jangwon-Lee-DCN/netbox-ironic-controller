from types import SimpleNamespace

import pytest

from netbox_ironic_controller.sync import IronicSyncClient


OWNER = "dcn"
PROJECT = "tenant-a"
NODE = "11111111-1111-1111-1111-111111111111"
IMAGE = "22222222-2222-2222-2222-222222222222"


class Baremetal:
    def __init__(self, state="available"):
        self.node = SimpleNamespace(
            id=NODE, owner=OWNER, lessee=PROJECT, provision_state=state,
            is_maintenance=False, last_error=None,
        )
        self.calls = []

    def get_node(self, node_uuid):
        assert node_uuid == NODE
        return self.node

    def update_node(self, node, **changes):
        self.calls.append(("update", changes))
        for name, value in changes.items(): setattr(node, name, value)
        return node

    def set_node_provision_state(self, node, target, **kwargs):
        self.calls.append(("provision", target, kwargs))
        return node

    def set_node_power_state(self, node, target, **kwargs):
        self.calls.append(("power", target, kwargs))


class Images:
    def get_image(self, image_id):
        assert image_id == IMAGE
        return SimpleNamespace(id=IMAGE, name="Ubuntu", checksum="abc", disk_format="qcow2")


def client(state="available"):
    value = object.__new__(IronicSyncClient)
    value.conn = SimpleNamespace(baremetal=Baremetal(state), image=Images())
    return value


async def test_deploy_pins_approved_glance_image_metadata_and_config_drive():
    runtime = client()
    config_drive = {"meta_data": {"local-hostname": "research-01"}, "user_data": ""}
    await runtime.deploy(NODE, PROJECT, IMAGE, config_drive, OWNER)
    assert runtime.conn.baremetal.calls[0] == ("update", {"instance_info": {
        "image_source": IMAGE, "image_checksum": "abc", "image_disk_format": "qcow2",
    }})
    assert runtime.conn.baremetal.calls[1][0:2] == ("provision", "active")
    assert runtime.conn.baremetal.calls[1][2]["config_drive"] == config_drive


async def test_power_action_maps_to_ironic_target_and_checks_lease():
    runtime = client("active")
    await runtime.set_power(NODE, PROJECT, "soft reboot", OWNER)
    assert runtime.conn.baremetal.calls == [
        ("power", "soft rebooting", {"wait": True, "timeout": 300}),
    ]
    runtime.conn.baremetal.node.lessee = "other"
    with pytest.raises(RuntimeError, match="owner/lessee"):
        await runtime.set_power(NODE, PROJECT, "on", OWNER)
