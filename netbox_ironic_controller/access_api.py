from __future__ import annotations

from asyncio import to_thread
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .access_auth import KeystoneTokenValidator
from .access_domain import AccessRequest, Actor, DomainError, RequestState
from .access_store import AccessStore
from .access_store import NodeAlreadyReserved, VersionConflict


router = APIRouter(prefix="/v1", tags=["baremetal-access"])


class RequestCreate(BaseModel):
    profile: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    quantity: int = Field(ge=1, le=16)
    purpose: str = Field(min_length=3, max_length=1000)
    lease_days: int = Field(ge=1)
    rack: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


class RequestView(BaseModel):
    id: str
    project_id: str
    profile: str
    quantity: int
    purpose: str
    requested_until: datetime
    rack: str | None
    state: RequestState
    nodes: list[str]
    version: int


class VersionedAction(BaseModel):
    version: int = Field(ge=0)


class DecisionAction(VersionedAction):
    reason: str = Field(min_length=1, max_length=1000)


async def current_actor(request: Request, x_auth_token: str = Header(default="")) -> Actor:
    if not getattr(request.app.state, "access_auth", None):
        raise HTTPException(status_code=503, detail="baremetal access is disabled")
    try:
        return await request.app.state.access_auth.validate(x_auth_token)
    except DomainError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def require_requester(actor: Actor) -> None:
    if not actor.roles.intersection({"baremetal_requester", "baremetal_operator", "baremetal_admin"}):
        raise HTTPException(status_code=403, detail="baremetal requester role is required")


def view(item: AccessRequest, actor: Actor, dcn_project_id: str) -> RequestView:
    is_admin = actor.project_id == dcn_project_id and "baremetal_admin" in actor.roles
    visible_states = {RequestState.LEASED, RequestState.RETURN_REQUESTED, RequestState.CLEANING, RequestState.ERROR}
    nodes = item.node_uuids if is_admin or (actor.project_id == item.project_id and item.state in visible_states) else []
    return RequestView(
        id=item.id, project_id=item.project_id, profile=item.profile, quantity=item.quantity,
        purpose=item.purpose, requested_until=item.requested_until, rack=item.rack,
        state=item.state, nodes=nodes, version=item.version,
    )


@router.post("/requests", response_model=RequestView, status_code=201)
async def create_request(payload: RequestCreate, request: Request,
                         actor: Actor = Depends(current_actor)) -> RequestView:
    require_requester(actor)
    settings = request.app.state.settings
    if payload.lease_days > settings.access_max_lease_days:
        raise HTTPException(status_code=422, detail="lease exceeds the configured maximum")
    item = AccessRequest(
        id=str(uuid4()), project_id=actor.project_id, user_id=actor.user_id,
        profile=payload.profile, quantity=payload.quantity, purpose=payload.purpose,
        requested_until=datetime.now(timezone.utc) + timedelta(days=payload.lease_days),
        rack=payload.rack,
    )
    await to_thread(request.app.state.access_store.create, item)
    return view(item, actor, settings.access_dcn_project_id)


@router.get("/requests", response_model=list[RequestView])
async def list_requests(request: Request, actor: Actor = Depends(current_actor)) -> list[RequestView]:
    require_requester(actor)
    settings = request.app.state.settings
    items = await to_thread(request.app.state.access_store.list_for_project, actor.project_id)
    return [view(item, actor, settings.access_dcn_project_id) for item in items]


@router.get("/admin/requests", response_model=list[RequestView])
async def list_admin_requests(request: Request,
                              actor: Actor = Depends(current_actor)) -> list[RequestView]:
    settings = request.app.state.settings
    try:
        actor.require_admin(settings.access_dcn_project_id)
    except DomainError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    items = await to_thread(request.app.state.access_store.list_all)
    return [view(item, actor, settings.access_dcn_project_id) for item in items]


@router.post("/admin/requests/{request_id}/approve", response_model=RequestView)
async def approve_request(request_id: str, payload: VersionedAction, request: Request,
                          actor: Actor = Depends(current_actor)) -> RequestView:
    settings = request.app.state.settings
    try:
        actor.require_admin(settings.access_dcn_project_id)
        item = await request.app.state.access_coordinator.approve(request_id, actor, payload.version)
    except (DomainError, VersionConflict, NodeAlreadyReserved) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return view(item, actor, settings.access_dcn_project_id)


@router.post("/requests/{request_id}/return", response_model=RequestView)
async def return_request(request_id: str, payload: VersionedAction, request: Request,
                         actor: Actor = Depends(current_actor)) -> RequestView:
    settings = request.app.state.settings
    try:
        require_requester(actor)
        item = await request.app.state.access_coordinator.return_lease(request_id, actor, payload.version)
    except (DomainError, VersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return view(item, actor, settings.access_dcn_project_id)


@router.post("/requests/{request_id}/cancel", response_model=RequestView)
async def cancel_request(request_id: str, payload: VersionedAction, request: Request,
                         actor: Actor = Depends(current_actor)) -> RequestView:
    settings = request.app.state.settings
    try:
        require_requester(actor)
        item = await request.app.state.access_coordinator.cancel(request_id, actor, payload.version)
    except (DomainError, VersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return view(item, actor, settings.access_dcn_project_id)


@router.post("/admin/requests/{request_id}/reject", response_model=RequestView)
async def reject_request(request_id: str, payload: DecisionAction, request: Request,
                         actor: Actor = Depends(current_actor)) -> RequestView:
    settings = request.app.state.settings
    try:
        actor.require_admin(settings.access_dcn_project_id)
        item = await request.app.state.access_coordinator.reject(
            request_id, actor, payload.reason, payload.version,
        )
    except (DomainError, VersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return view(item, actor, settings.access_dcn_project_id)


def configure_access(app, settings, netbox, ironic) -> None:
    from .access_inventory import IronicLeaseAdapter, NetBoxIronicOfferInventory
    from .access_service import AccessCoordinator

    app.state.access_store = AccessStore(settings.access_database_path)
    app.state.access_auth = KeystoneTokenValidator(settings)
    app.state.access_coordinator = AccessCoordinator(
        app.state.access_store,
        NetBoxIronicOfferInventory(netbox, ironic),
        IronicLeaseAdapter(ironic, settings.access_dcn_project_id),
        settings.access_dcn_project_id,
    )
