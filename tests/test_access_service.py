from datetime import datetime, timedelta, timezone

import pytest

from netbox_ironic_controller.access_domain import AccessRequest, Actor, OfferCandidate, RequestState
from netbox_ironic_controller.access_service import AccessCoordinator
from netbox_ironic_controller.access_store import AccessStore


ADMIN = Actor("admin", "dcn", frozenset({"baremetal_admin"}))
REQUESTER = Actor("user", "tenant-a", frozenset({"baremetal_operator"}))


class Inventory:
    async def candidates(self, profile, rack):
        return [OfferCandidate("node-1", "rack-1", profile, True, True, "available", False, None, None)]


class Runtime:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def assign_lessee(self, node_uuid, project_id):
        self.calls.append(("assign", node_uuid, project_id))
        if self.fail == "assign": raise RuntimeError("Ironic unavailable")

    async def return_and_clean(self, node_uuid):
        self.calls.append(("clean", node_uuid))
        if self.fail == "clean": raise RuntimeError("clean failed")

    async def clear_lessee(self, node_uuid):
        self.calls.append(("clear", node_uuid))

    async def deploy(self, node_uuid, project_id, image_id, config_drive):
        self.calls.append(("deploy", node_uuid, project_id, image_id, config_drive))

    async def set_power(self, node_uuid, project_id, action):
        self.calls.append(("power", node_uuid, project_id, action))

    async def approved_images(self):
        return [{"id": "image-1", "name": "Ubuntu"}]


def seed(store, until=None):
    item = AccessRequest("req", "tenant-a", "user", "general", 1, "research",
                         until or datetime.now(timezone.utc) + timedelta(days=1))
    store.create(item)
    return item


async def test_approve_sets_lessee_and_persists_leased(tmp_path):
    store, runtime = AccessStore(tmp_path / "db"), Runtime()
    seed(store)
    item = await AccessCoordinator(store, Inventory(), runtime, "dcn").approve("req", ADMIN)
    assert item.state == RequestState.LEASED
    assert runtime.calls == [("assign", "node-1", "tenant-a")]
    assert store.get("req").state == RequestState.LEASED


async def test_assignment_failure_quarantines_reservation(tmp_path):
    store, runtime = AccessStore(tmp_path / "db"), Runtime("assign")
    seed(store)
    with pytest.raises(RuntimeError):
        await AccessCoordinator(store, Inventory(), runtime, "dcn").approve("req", ADMIN)
    assert store.get("req").state == RequestState.ERROR
    assert "Ironic unavailable" in store.get("req").decision_reason


async def test_return_cleans_before_clearing_lessee_and_releasing_node(tmp_path):
    store, runtime = AccessStore(tmp_path / "db"), Runtime()
    seed(store)
    service = AccessCoordinator(store, Inventory(), runtime, "dcn")
    await service.approve("req", ADMIN)
    item = await service.return_lease("req", REQUESTER)
    assert item.state == RequestState.RETURNED
    assert runtime.calls[-2:] == [("clean", "node-1"), ("clear", "node-1")]


async def test_expiry_uses_return_and_cleaning_path(tmp_path):
    store, runtime = AccessStore(tmp_path / "db"), Runtime()
    seed(store, datetime.now(timezone.utc) - timedelta(seconds=1))
    service = AccessCoordinator(store, Inventory(), runtime, "dcn")
    await service.approve("req", ADMIN)
    assert await service.expire_leases(ADMIN) == ["req"]
    assert store.get("req").state == RequestState.RETURNED


async def test_only_lessee_operator_can_deploy_and_power_assigned_node(tmp_path):
    store, runtime = AccessStore(tmp_path / "db"), Runtime()
    seed(store)
    service = AccessCoordinator(store, Inventory(), runtime, "dcn")
    leased = await service.approve("req", ADMIN)
    await service.deploy(
        "req", REQUESTER, "node-1", "image-1",
        {"meta_data": {"local-hostname": "node"}}, leased.version,
    )
    await service.set_power("req", REQUESTER, "node-1", "reboot", leased.version)
    assert runtime.calls[-2][0:4] == ("deploy", "node-1", "tenant-a", "image-1")
    assert runtime.calls[-1] == ("power", "node-1", "tenant-a", "reboot")
    assert [event["action"] for event in store.audit_events("req")][-2:] == [
        "deploy:node-1:image-1", "power:node-1:reboot",
    ]


async def test_requester_without_operator_role_cannot_mutate_node(tmp_path):
    store, runtime = AccessStore(tmp_path / "db"), Runtime()
    seed(store)
    service = AccessCoordinator(store, Inventory(), runtime, "dcn")
    leased = await service.approve("req", ADMIN)
    viewer = Actor("viewer", "tenant-a", frozenset({"baremetal_requester"}))
    with pytest.raises(ValueError, match="baremetal_operator"):
        await service.set_power("req", viewer, "node-1", "reboot", leased.version)
