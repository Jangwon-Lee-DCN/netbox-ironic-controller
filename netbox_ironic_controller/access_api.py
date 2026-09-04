from __future__ import annotations

from asyncio import to_thread
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .access_auth import KeystoneTokenValidator
from .access_domain import AccessRequest, Actor, DomainError, RequestState
from .access_store import AccessStore
from .access_store import NodeAlreadyReserved, VersionConflict
from .access_operations import NodeOperation, OperationState


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


class OfferView(BaseModel):
    profile: str
    rack: str
    available: int
    max_lease_days: int


class DeployImageView(BaseModel):
    id: str
    name: str


class OperationView(BaseModel):
    id: str
    request_id: str
    node_uuid: str
    operation: str
    state: OperationState
    error: str | None
    created_at: datetime
    updated_at: datetime


class VersionedAction(BaseModel):
    version: int = Field(ge=0)


class DecisionAction(VersionedAction):
    reason: str = Field(min_length=1, max_length=1000)


class DeployAction(VersionedAction):
    node_uuid: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    image_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    hostname: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,62}$")
    user_data: str = Field(default="", max_length=65536)


class PowerAction(VersionedAction):
    node_uuid: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    action: str = Field(pattern=r"^(on|off|reboot|soft off|soft reboot)$")


async def current_actor(request: Request, x_auth_token: str = Header(default="")) -> Actor:
    if not getattr(request.app.state, "access_auth", None):
        raise HTTPException(status_code=503, detail="baremetal access is disabled")
    try:
        return await request.app.state.access_auth.validate(x_auth_token)
    except DomainError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def require_requester(actor: Actor, dcn_domain_id: str) -> None:
    if not actor.can_request(dcn_domain_id):
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


def operation_view(item: NodeOperation) -> OperationView:
    return OperationView(
        id=item.id, request_id=item.request_id, node_uuid=item.node_uuid,
        operation=item.operation, state=item.state, error=item.error,
        created_at=item.created_at, updated_at=item.updated_at,
    )


def start_operation(request: Request, operation_id: str) -> None:
    task = asyncio.create_task(request.app.state.access_coordinator.process_operation(operation_id))
    tasks = getattr(request.app.state, "access_tasks", None)
    if tasks is None:
        tasks = set()
        request.app.state.access_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)


@router.post("/requests", response_model=RequestView, status_code=201)
async def create_request(payload: RequestCreate, request: Request,
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                         actor: Actor = Depends(current_actor)) -> RequestView:
    settings = request.app.state.settings
    require_requester(actor, settings.access_dcn_domain_id)
    if payload.lease_days > settings.access_max_lease_days:
        raise HTTPException(status_code=422, detail="lease exceeds the configured maximum")
    item = AccessRequest(
        id=str(uuid4()), project_id=actor.project_id, user_id=actor.user_id,
        profile=payload.profile, quantity=payload.quantity, purpose=payload.purpose,
        requested_until=datetime.now(timezone.utc) + timedelta(days=payload.lease_days),
        rack=payload.rack,
    )
    if idempotency_key is not None and not (8 <= len(idempotency_key) <= 128):
        raise HTTPException(status_code=422, detail="invalid Idempotency-Key")
    item = await to_thread(request.app.state.access_store.create_idempotent, item, idempotency_key)
    return view(item, actor, settings.access_dcn_project_id)


@router.get("/requests", response_model=list[RequestView])
async def list_requests(request: Request, actor: Actor = Depends(current_actor)) -> list[RequestView]:
    settings = request.app.state.settings
    require_requester(actor, settings.access_dcn_domain_id)
    items = await to_thread(request.app.state.access_store.list_for_project, actor.project_id)
    return [view(item, actor, settings.access_dcn_project_id) for item in items]


@router.get("/requests/{request_id}", response_model=RequestView)
async def get_request(request_id: str, request: Request,
                      actor: Actor = Depends(current_actor)) -> RequestView:
    settings = request.app.state.settings
    require_requester(actor, settings.access_dcn_domain_id)
    try:
        item = await to_thread(request.app.state.access_store.get, request_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="request not found") from exc
    if item.project_id != actor.project_id:
        raise HTTPException(status_code=404, detail="request not found")
    return view(item, actor, settings.access_dcn_project_id)


@router.get("/offers", response_model=list[OfferView])
async def list_offers(request: Request, actor: Actor = Depends(current_actor)) -> list[OfferView]:
    require_requester(actor, request.app.state.settings.access_dcn_domain_id)
    candidates = await request.app.state.access_coordinator.inventory.candidates(None, None)
    grouped: dict[tuple[str, str], list] = {}
    for candidate in candidates:
        if candidate.eligible:
            grouped.setdefault((candidate.profile, candidate.rack), []).append(candidate)
    return [
        OfferView(
            profile=profile, rack=rack, available=len(rows),
            max_lease_days=min(row.max_lease_days for row in rows),
        )
        for (profile, rack), rows in sorted(grouped.items())
    ]


@router.get("/deploy-images", response_model=list[DeployImageView])
async def list_deploy_images(request: Request,
                             actor: Actor = Depends(current_actor)) -> list[DeployImageView]:
    require_requester(actor, request.app.state.settings.access_dcn_domain_id)
    rows = await request.app.state.access_coordinator.runtime.approved_images()
    return [DeployImageView(**row) for row in rows]


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


@router.post("/requests/{request_id}/return", response_model=RequestView, status_code=202)
async def return_request(request_id: str, payload: VersionedAction, request: Request,
                         background_tasks: BackgroundTasks,
                         actor: Actor = Depends(current_actor)) -> RequestView:
    settings = request.app.state.settings
    require_requester(actor, settings.access_dcn_domain_id)
    if not actor.can_operate():
        raise HTTPException(status_code=403, detail="baremetal operator role is required")
    try:
        coordinator = request.app.state.access_coordinator
        item = await coordinator.begin_return(request_id, actor, payload.version)
        background_tasks.add_task(coordinator.complete_return, request_id, actor, False)
    except (DomainError, VersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return view(item, actor, settings.access_dcn_project_id)


@router.post("/requests/{request_id}/cancel", response_model=RequestView)
async def cancel_request(request_id: str, payload: VersionedAction, request: Request,
                         actor: Actor = Depends(current_actor)) -> RequestView:
    settings = request.app.state.settings
    try:
        require_requester(actor, settings.access_dcn_domain_id)
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


@router.post("/requests/{request_id}/deploy", response_model=OperationView, status_code=202)
async def deploy_node(request_id: str, payload: DeployAction, request: Request,
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                      actor: Actor = Depends(current_actor)) -> OperationView:
    require_requester(actor, request.app.state.settings.access_dcn_domain_id)
    if not actor.can_operate():
        raise HTTPException(status_code=403, detail="baremetal operator role is required")
    try:
        if idempotency_key is not None and not (8 <= len(idempotency_key) <= 128):
            raise ValueError("invalid Idempotency-Key")
        item = await request.app.state.access_coordinator.queue_deploy(
            request_id, actor, payload.node_uuid, payload.image_id,
            {"meta_data": {"instance-id": payload.node_uuid, "local-hostname": payload.hostname},
             "user_data": payload.user_data}, payload.version, idempotency_key,
        )
    except (DomainError, ValueError, RuntimeError, VersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    start_operation(request, item.id)
    return operation_view(item)


@router.post("/requests/{request_id}/power", response_model=OperationView, status_code=202)
async def power_node(request_id: str, payload: PowerAction, request: Request,
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                     actor: Actor = Depends(current_actor)) -> OperationView:
    require_requester(actor, request.app.state.settings.access_dcn_domain_id)
    if not actor.can_operate():
        raise HTTPException(status_code=403, detail="baremetal operator role is required")
    try:
        if idempotency_key is not None and not (8 <= len(idempotency_key) <= 128):
            raise ValueError("invalid Idempotency-Key")
        item = await request.app.state.access_coordinator.queue_power(
            request_id, actor, payload.node_uuid, payload.action, payload.version, idempotency_key,
        )
    except (DomainError, ValueError, RuntimeError, VersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    start_operation(request, item.id)
    return operation_view(item)


@router.get("/requests/{request_id}/operations", response_model=list[OperationView])
async def list_node_operations(request_id: str, request: Request,
                               actor: Actor = Depends(current_actor)) -> list[OperationView]:
    require_requester(actor, request.app.state.settings.access_dcn_domain_id)
    try:
        item = await to_thread(request.app.state.access_store.get, request_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="request not found") from exc
    if item.project_id != actor.project_id:
        raise HTTPException(status_code=404, detail="request not found")
    rows = await to_thread(
        request.app.state.access_store.list_operations, request_id, actor.project_id,
    )
    return [operation_view(row) for row in rows]


def configure_access(app, settings, netbox, ironic) -> None:
    import json
    from .access_inventory import IronicLeaseAdapter, NetBoxIronicOfferInventory
    from .access_service import AccessCoordinator

    app.state.access_store = AccessStore(
        settings.access_database_url or settings.access_database_path
    )
    app.state.access_tasks = set()
    app.state.access_auth = KeystoneTokenValidator(settings)
    app.state.access_coordinator = AccessCoordinator(
        app.state.access_store,
        NetBoxIronicOfferInventory(netbox, ironic),
        IronicLeaseAdapter(
            ironic, netbox, settings.access_dcn_project_id,
            deploy_image_ids={value.strip() for value in settings.access_deploy_image_ids.split(",") if value.strip()},
            clean_steps=json.loads(settings.access_clean_steps_json),
            deploy_images=json.loads(settings.access_deploy_images_json),
        ),
        settings.access_dcn_project_id,
    )
