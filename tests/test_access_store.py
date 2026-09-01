from datetime import datetime, timedelta, timezone

import pytest

from netbox_ironic_controller.access_domain import AccessRequest, Actor, OfferCandidate
from netbox_ironic_controller.access_store import AccessStore, NodeAlreadyReserved, VersionConflict


ADMIN = Actor("admin", "dcn", frozenset({"baremetal_admin"}))


def make_request(request_id, project):
    return AccessRequest(request_id, project, "user", "general", 1, "research",
                         datetime.now(timezone.utc) + timedelta(days=2))


def node():
    return OfferCandidate("node-1", "rack-1", "general", True, True, "available", False, None, None)


def test_project_listing_never_returns_other_projects(tmp_path):
    store = AccessStore(tmp_path / "access.db")
    store.create(make_request("a", "project-a"))
    store.create(make_request("b", "project-b"))
    assert [item.id for item in store.list_for_project("project-a")] == ["a"]


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
