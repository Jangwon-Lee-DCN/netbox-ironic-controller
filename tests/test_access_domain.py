from datetime import datetime, timedelta, timezone

import pytest

from netbox_ironic_controller.access_domain import (
    AccessRequest, Actor, DomainError, OfferCandidate, RequestState,
)


DCN = "dcn-project"
ADMIN = Actor("admin-user", DCN, frozenset({"baremetal_admin"}))
REQUESTER = Actor("requester", "tenant-a", frozenset({"baremetal_requester"}))


def candidate(**changes):
    values = dict(node_uuid="node-1", rack="rack-1", profile="general-1u",
                  netbox_active=True, offer_enabled=True, provision_state="available",
                  maintenance=False, lessee=None, last_error=None)
    values.update(changes)
    return OfferCandidate(**values)


def request():
    return AccessRequest("req-1", "tenant-a", "requester", "general-1u", 1,
                         "research", datetime.now(timezone.utc) + timedelta(days=7))


def test_only_dcn_baremetal_admin_can_approve():
    item = request()
    with pytest.raises(DomainError, match="dcn baremetal_admin"):
        item.approve(REQUESTER, DCN, [candidate()])


@pytest.mark.parametrize("changes", [
    {"netbox_active": False}, {"offer_enabled": False}, {"provision_state": "active"},
    {"maintenance": True}, {"lessee": "another-project"}, {"last_error": "BMC failed"},
])
def test_offer_eligibility_is_fail_closed(changes):
    assert not candidate(**changes).eligible


def test_approval_selects_only_matching_profile_and_rack():
    item = request()
    item.rack = "rack-1"
    item.approve(ADMIN, DCN, [candidate(node_uuid="wrong-rack", rack="rack-2"), candidate()])
    assert item.state == RequestState.APPROVED
    assert item.node_uuids == ["node-1"]


def test_lease_return_requires_cleaning_before_returned():
    item = request()
    item.approve(ADMIN, DCN, [candidate()])
    item.allocation_started()
    item.allocation_completed()
    item.request_return(REQUESTER, DCN)
    with pytest.raises(DomainError):
        item.cleaning_completed()
    item.cleaning_started()
    item.cleaning_completed()
    assert item.state == RequestState.RETURNED


def test_expired_lease_uses_the_same_return_path():
    item = request()
    item.requested_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    item.approve(ADMIN, DCN, [candidate()])
    item.allocation_started()
    item.allocation_completed()
    assert item.expired()
    item.request_return(ADMIN, DCN)
    assert item.state == RequestState.RETURN_REQUESTED


def test_admin_role_outside_dcn_cannot_return_another_projects_node():
    item = request()
    item.approve(ADMIN, DCN, [candidate()])
    item.allocation_started()
    item.allocation_completed()
    impostor = Actor("other-admin", "other-project", frozenset({"baremetal_admin"}))
    with pytest.raises(DomainError, match="requester project or"):
        item.request_return(impostor, DCN)


def test_request_can_be_rejected_or_cancelled_only_before_allocation():
    rejected = request()
    rejected.reject(ADMIN, DCN, "capacity reserved for maintenance")
    assert rejected.state == RequestState.REJECTED
    assert rejected.decision_reason == "capacity reserved for maintenance"
    cancelled = request()
    cancelled.cancel(REQUESTER)
    assert cancelled.state == RequestState.CANCELLED
