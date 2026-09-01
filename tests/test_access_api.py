from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import asyncio

from netbox_ironic_controller.access_api import router
from netbox_ironic_controller.access_domain import Actor
from netbox_ironic_controller.access_store import AccessStore
from netbox_ironic_controller.access_service import AccessCoordinator
from netbox_ironic_controller.access_domain import OfferCandidate
from netbox_ironic_controller.config import Settings

NODE_ID = "22222222-2222-2222-2222-222222222222"
IMAGE_ID = "11111111-1111-1111-1111-111111111111"

class Auth:
    async def validate(self, token):
        actors = {
            "tenant-a": Actor("user-a", "project-a", frozenset({"baremetal_requester"})),
            "tenant-b": Actor("user-b", "project-b", frozenset({"baremetal_requester"})),
            "tenant-op": Actor("operator-a", "project-a", frozenset({"baremetal_operator"})),
            "admin": Actor("admin", "dcn", frozenset({"baremetal_admin"})),
            "member": Actor("member", "project-a", frozenset({"member"})),
        }
        return actors[token]


class Inventory:
    async def candidates(self, profile, rack):
        return [OfferCandidate(NODE_ID, "Rack 1", profile or "general-1u", True, True, "available", False, None, None)]


class Runtime:
    def __init__(self): self.operations = []
    async def assign_lessee(self, node_uuid, project_id): pass
    async def return_and_clean(self, node_uuid): pass
    async def clear_lessee(self, node_uuid): pass
    async def approved_images(self): return [{"id": IMAGE_ID, "name": "Ubuntu"}]
    async def deploy(self, node_uuid, project_id, image_id, config_drive):
        self.operations.append(("deploy", node_uuid, project_id, image_id))
    async def set_power(self, node_uuid, project_id, action):
        self.operations.append(("power", node_uuid, project_id, action))


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


async def test_offer_list_is_sanitized_and_contains_no_node_identity(tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app(tmp_path)), base_url="http://test") as api:
        response = await api.get("/v1/offers", headers={"X-Auth-Token": "tenant-a"})
    assert response.status_code == 200
    assert response.json() == [{
        "profile": "general-1u", "rack": "Rack 1", "available": 1, "max_lease_days": 30,
    }]
    assert NODE_ID not in response.text


async def test_deploy_image_catalog_contains_only_approved_identity(tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app(tmp_path)), base_url="http://test") as api:
        response = await api.get("/v1/deploy-images", headers={"X-Auth-Token": "tenant-a"})
    assert response.status_code == 200
    assert response.json() == [{
        "id": IMAGE_ID, "name": "Ubuntu",
    }]


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
        assert approved["nodes"] == [NODE_ID]
        own = (await api.get("/v1/requests", headers={"X-Auth-Token": "tenant-a"})).json()
        other = (await api.get("/v1/requests", headers={"X-Auth-Token": "tenant-b"})).json()
    assert own[0]["nodes"] == [NODE_ID]
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


async def test_only_lessee_operator_can_call_deploy_and_power_endpoints(tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app(tmp_path)), base_url="http://test") as api:
        created = (await submit(api)).json()
        leased = (await api.post(
            f"/v1/admin/requests/{created['id']}/approve", headers={"X-Auth-Token": "admin"},
            json={"version": created["version"]},
        )).json()
        payload = {
            "version": leased["version"], "node_uuid": NODE_ID, "image_id": IMAGE_ID,
            "hostname": "research-01", "user_data": "#cloud-config\n",
        }
        assert (await api.post(
            f"/v1/requests/{created['id']}/deploy",
            headers={"X-Auth-Token": "tenant-a"}, json=payload,
        )).status_code == 409
        deploy = await api.post(
            f"/v1/requests/{created['id']}/deploy",
            headers={"X-Auth-Token": "tenant-op", "Idempotency-Key": "deploy-operation-key"},
            json=payload,
        )
        assert deploy.status_code == 202
        assert deploy.json()["operation"] == "deploy"
        duplicate = await api.post(
            f"/v1/requests/{created['id']}/deploy",
            headers={"X-Auth-Token": "tenant-op", "Idempotency-Key": "deploy-operation-key"},
            json=payload,
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["id"] == deploy.json()["id"]
        for _ in range(100):
            operations = await api.get(
                f"/v1/requests/{created['id']}/operations",
                headers={"X-Auth-Token": "tenant-op"},
            )
            if operations.json()[0]["state"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert operations.json()[0]["state"] == "succeeded"
        power = await api.post(
            f"/v1/requests/{created['id']}/power",
            headers={"X-Auth-Token": "tenant-op"},
            json={"version": leased["version"], "node_uuid": NODE_ID, "action": "reboot"},
        )
        assert power.status_code == 202
        for _ in range(100):
            operations = await api.get(
                f"/v1/requests/{created['id']}/operations",
                headers={"X-Auth-Token": "tenant-op"},
            )
            if len(operations.json()) == 2 and operations.json()[1]["state"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert operations.status_code == 200
        assert [item["operation"] for item in operations.json()] == ["deploy", "power"]
        assert all(item["state"] == "succeeded" for item in operations.json())
