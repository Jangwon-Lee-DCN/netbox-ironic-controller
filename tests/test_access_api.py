from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from netbox_ironic_controller.access_api import router
from netbox_ironic_controller.access_domain import Actor
from netbox_ironic_controller.access_store import AccessStore
from netbox_ironic_controller.access_service import AccessCoordinator
from netbox_ironic_controller.access_domain import OfferCandidate
from netbox_ironic_controller.config import Settings


class Auth:
    async def validate(self, token):
        actors = {
            "tenant-a": Actor("user-a", "project-a", frozenset({"baremetal_requester"})),
            "tenant-b": Actor("user-b", "project-b", frozenset({"baremetal_requester"})),
            "admin": Actor("admin", "dcn", frozenset({"baremetal_admin"})),
            "member": Actor("member", "project-a", frozenset({"member"})),
        }
        return actors[token]


class Inventory:
    async def candidates(self, profile, rack):
        return [OfferCandidate("node-1", "Rack 1", profile, True, True, "available", False, None, None)]


class Runtime:
    async def assign_lessee(self, node_uuid, project_id): pass
    async def return_and_clean(self, node_uuid): pass
    async def clear_lessee(self, node_uuid): pass


def app(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings(access_dcn_project_id="dcn", access_max_lease_days=30)
    app.state.access_store = AccessStore(tmp_path / "api.db")
    app.state.access_auth = Auth()
    app.state.access_coordinator = AccessCoordinator(app.state.access_store, Inventory(), Runtime(), "dcn")
    return app


async def submit(client, token="tenant-a"):
    return await client.post("/v1/requests", headers={"X-Auth-Token": token}, json={
        "profile": "general-1u", "quantity": 1, "purpose": "research",
        "lease_days": 7, "rack": "Rack 1",
    })


async def test_requester_sees_only_own_project(tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app(tmp_path)), base_url="http://test") as api:
        assert (await submit(api, "tenant-a")).status_code == 201
        assert (await submit(api, "tenant-b")).status_code == 201
        rows = (await api.get("/v1/requests", headers={"X-Auth-Token": "tenant-a"})).json()
    assert len(rows) == 1
    assert rows[0]["project_id"] == "project-a"


async def test_only_dcn_admin_can_list_all_requests(tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app(tmp_path)), base_url="http://test") as api:
        await submit(api)
        assert (await api.get("/v1/admin/requests", headers={"X-Auth-Token": "tenant-a"})).status_code == 403
        rows = (await api.get("/v1/admin/requests", headers={"X-Auth-Token": "admin"})).json()
    assert len(rows) == 1


async def test_plain_member_and_excessive_lease_are_rejected(tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app(tmp_path)), base_url="http://test") as api:
        assert (await submit(api, "member")).status_code == 403
        response = await api.post("/v1/requests", headers={"X-Auth-Token": "tenant-a"}, json={
            "profile": "general-1u", "quantity": 1, "purpose": "research", "lease_days": 31,
        })
    assert response.status_code == 422


async def test_approved_node_is_visible_only_to_admin_and_lessee_project(tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app(tmp_path)), base_url="http://test") as api:
        created = (await submit(api, "tenant-a")).json()
        approved = (await api.post(
            f"/v1/admin/requests/{created['id']}/approve",
            headers={"X-Auth-Token": "admin"}, json={"version": created["version"]},
        )).json()
        assert approved["nodes"] == ["node-1"]
        own = (await api.get("/v1/requests", headers={"X-Auth-Token": "tenant-a"})).json()
        other = (await api.get("/v1/requests", headers={"X-Auth-Token": "tenant-b"})).json()
    assert own[0]["nodes"] == ["node-1"]
    assert other == []


async def test_admin_reject_and_requester_cancel_are_versioned(tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app(tmp_path)), base_url="http://test") as api:
        first = (await submit(api)).json()
        rejected = await api.post(
            f"/v1/admin/requests/{first['id']}/reject",
            headers={"X-Auth-Token": "admin"},
            json={"version": first["version"], "reason": "maintenance window"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["state"] == "rejected"
        second = (await submit(api)).json()
        cancelled = await api.post(
            f"/v1/requests/{second['id']}/cancel",
            headers={"X-Auth-Token": "tenant-a"}, json={"version": second["version"]},
        )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
