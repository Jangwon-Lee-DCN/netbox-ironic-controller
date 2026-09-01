import pytest

from netbox_ironic_controller.access_auth import actor_from_token_body
from netbox_ironic_controller.access_domain import DomainError


def test_project_scoped_token_becomes_actor():
    actor = actor_from_token_body({"token": {
        "user": {"id": "user-1"}, "project": {"id": "project-1"},
        "roles": [{"name": "member"}, {"name": "baremetal_requester"}],
    }})
    assert actor.project_id == "project-1"
    assert actor.roles == frozenset({"member", "baremetal_requester"})


def test_system_or_domain_token_is_rejected_for_requester_api():
    with pytest.raises(DomainError, match="project-scoped"):
        actor_from_token_body({"token": {"user": {"id": "user-1"}, "roles": []}})
