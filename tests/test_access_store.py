from datetime import datetime, timedelta, timezone

import pytest

from netbox_ironic_controller.access_domain import AccessRequest, Actor, DomainError, OfferCandidate
from netbox_ironic_controller.access_operations import NodeOperation, OperationState
from netbox_ironic_controller.access_store import AccessStore, NodeAlreadyReserved, VersionConflict


ADMIN = Actor("admin", "dcn", frozenset({"baremetal_admin"}))


def make_request(request_id, project):
    return AccessRequest(request_id, project, "user", "general", 1, "research",
                         datetime.now(timezone.utc) + timedelta(days=2))


def node():
    return OfferCandidate("node-1", "rack-1", "general", True, True, "available", False, None, None)


def operation(operation_id="op-1", state=OperationState.QUEUED):
    now = datetime.now(timezone.utc)
    return NodeOperation(operation_id, "a", "project-a", "user", "node-1", "power",
                         {"action": "reboot"}, state, None, now, now)


def test_project_listing_never_returns_other_projects(tmp_path):
    store = AccessStore(tmp_path / "access.db")
    store.create(make_request("a", "project-a"))
    store.create(make_request("b", "project-b"))
    assert [item.id for item in store.list_for_project("project-a")] == ["a"]
    created = store.audit_events("a")
    assert created[0]["action"] == "created"
    assert created[0]["before_json"] == "{}"


def test_node_reservation_is_unique_across_requests(tmp_path):
    store = AccessStore(tmp_path / "access.db")
    first, second = make_request("a", "project-a"), make_request("b", "project-b")
    store.create(first)
    store.create(second)
    first.approve(ADMIN, "dcn", [node()])
    store.save_with_reservations(first, 0, ADMIN.user_id, ADMIN.project_id)
    second.approve(ADMIN, "dcn", [node()])
    with pytest.raises(NodeAlreadyReserved):
        store.save_with_reservations(second, 0, ADMIN.user_id, ADMIN.project_id)


def test_stale_version_cannot_overwrite_newer_decision(tmp_path):
    store = AccessStore(tmp_path / "access.db")
    original = make_request("a", "project-a")
    store.create(original)
    first = store.get("a")
    stale = store.get("a")
    first.begin_review(ADMIN, "dcn")
    store.save(first, 0, ADMIN.user_id, ADMIN.project_id, "review")
    stale.begin_review(ADMIN, "dcn")
    with pytest.raises(VersionConflict):
        store.save(stale, 0, ADMIN.user_id, ADMIN.project_id, "review")


def test_request_creation_is_idempotent_within_project(tmp_path):
    store = AccessStore(tmp_path / "access.db")
    first = store.create_idempotent(make_request("a", "project-a"), "request-key-123")
    duplicate = store.create_idempotent(make_request("b", "project-a"), "request-key-123")
    other_project = store.create_idempotent(make_request("c", "project-b"), "request-key-123")
    assert duplicate.id == first.id == "a"
    assert other_project.id == "c"


def test_reservation_released_only_after_cleaning_completed(tmp_path):
    store = AccessStore(tmp_path / "access.db")
    item = make_request("a", "project-a")
    store.create(item)
    item.approve(ADMIN, "dcn", [node()])
    store.save_with_reservations(item, 0, ADMIN.user_id, ADMIN.project_id)
    item.allocation_started(); item.allocation_completed()
    store.save(item, 1, ADMIN.user_id, ADMIN.project_id, "leased")
    item.request_return(ADMIN, "dcn"); item.cleaning_started(); item.cleaning_completed()
    store.save(item, 3, ADMIN.user_id, ADMIN.project_id, "cleaned", release_reservations=True)
    replacement = make_request("b", "project-b")
    store.create(replacement)
    replacement.approve(ADMIN, "dcn", [node()])
    store.save_with_reservations(replacement, 0, ADMIN.user_id, ADMIN.project_id)
    assert store.audit_events("a")[-1]["action"] == "cleaned"


def test_node_operation_state_and_active_node_are_transactional(tmp_path):
    store = AccessStore(tmp_path / "access.db")
    store.create(make_request("a", "project-a"))
    store.create_operation(operation())
    with pytest.raises(DomainError, match="already has an active operation"):
        store.create_operation(operation("op-2"))
    assert store.has_active_operations("a")
    assert store.start_operation("op-1").state == OperationState.RUNNING
    assert store.finish_operation("op-1").state == OperationState.SUCCEEDED
    assert not store.has_active_operations("a")


def test_service_restart_fails_running_operation_closed(tmp_path):
    path = tmp_path / "access.db"
    store = AccessStore(path)
    store.create(make_request("a", "project-a"))
    store.create_operation(operation())
    store.start_operation("op-1")
    recovered = AccessStore(path).get_operation("op-1")
    assert recovered.state == OperationState.FAILED
    assert "operator reconciliation required" in recovered.error


def test_service_restart_fails_unstarted_queued_operation_closed(tmp_path):
    path = tmp_path / "access.db"
    store = AccessStore(path)
    store.create(make_request("a", "project-a"))
    store.create_operation(operation())
    recovered = AccessStore(path).get_operation("op-1")
    assert recovered.state == OperationState.FAILED
    assert not AccessStore(path).has_active_operations("a")


def test_node_operation_creation_is_idempotent_and_payload_bound(tmp_path):
    store = AccessStore(tmp_path / "access.db")
    store.create(make_request("a", "project-a"))
    first = store.create_operation_idempotent(operation(), "operation-key")
    duplicate = store.create_operation_idempotent(operation("op-2"), "operation-key")
    assert duplicate.id == first.id == "op-1"
    changed = operation("op-3")
    changed.payload = {"action": "off"}
    with pytest.raises(DomainError, match="reused with a different operation"):
        store.create_operation_idempotent(changed, "operation-key")
