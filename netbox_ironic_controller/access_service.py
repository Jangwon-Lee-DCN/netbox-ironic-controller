from __future__ import annotations

from asyncio import to_thread
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from .access_domain import AccessRequest, Actor, DomainError, OfferCandidate, RequestState
from .access_store import AccessStore
from .access_operations import NodeOperation, OperationState


class Inventory(Protocol):
    async def candidates(self, profile: str, rack: str | None) -> list[OfferCandidate]: ...


class IronicLeaseRuntime(Protocol):
    async def assign_lessee(self, node_uuid: str, project_id: str) -> None: ...
    async def return_and_clean(self, node_uuid: str) -> None: ...
    async def clear_lessee(self, node_uuid: str) -> None: ...
    async def deploy(self, node_uuid: str, project_id: str, image_id: str,
                     config_drive: dict) -> None: ...
    async def set_power(self, node_uuid: str, project_id: str, action: str) -> None: ...
    async def approved_images(self) -> list[dict]: ...


class AccessCoordinator:
    def __init__(self, store: AccessStore, inventory: Inventory, runtime: IronicLeaseRuntime,
                 dcn_project_id: str):
        self.store = store
        self.inventory = inventory
        self.runtime = runtime
        self.dcn_project_id = dcn_project_id

    async def approve(self, request_id: str, actor: Actor,
                      expected_version: int | None = None) -> AccessRequest:
        item = await to_thread(self.store.get, request_id)
        if expected_version is not None and item.version != expected_version:
            from .access_store import VersionConflict
            raise VersionConflict(f"expected version {expected_version}, got {item.version}")
        expected = item.version
        candidates = await self.inventory.candidates(item.profile, item.rack)
        item.approve(actor, self.dcn_project_id, candidates)
        await to_thread(self.store.save_with_reservations, item, expected,
                        actor.user_id, actor.project_id, "approve")

        expected = item.version
        item.allocation_started()
        await to_thread(self.store.save, item, expected, actor.user_id, actor.project_id, "allocation_started")
        assigned: list[str] = []
        try:
            for node_uuid in item.node_uuids:
                await self.runtime.assign_lessee(node_uuid, item.project_id)
                assigned.append(node_uuid)
        except Exception as exc:
            # Reservations remain intentionally held. Partial external mutation is quarantined
            # until an administrator reconciles it; automatic reuse would risk cross-project access.
            expected = item.version
            item.mark_error(f"lessee assignment failed after {len(assigned)} nodes: {exc}")
            await to_thread(self.store.save, item, expected, actor.user_id, actor.project_id,
                            "allocation_failed")
            raise
        expected = item.version
        item.allocation_completed()
        await to_thread(self.store.save, item, expected, actor.user_id, actor.project_id,
                        "allocation_completed")
        return item

    async def return_lease(self, request_id: str, actor: Actor,
                           expected_version: int | None = None) -> AccessRequest:
        item = await to_thread(self.store.get, request_id)
        if await to_thread(self.store.has_active_operations, request_id):
            raise DomainError("cannot return a lease while a node operation is active")
        if expected_version is not None and item.version != expected_version:
            from .access_store import VersionConflict
            raise VersionConflict(f"expected version {expected_version}, got {item.version}")
        expected = item.version
        item.request_return(actor, self.dcn_project_id)
        await to_thread(self.store.save, item, expected, actor.user_id, actor.project_id,
                        "return_requested")
        expected = item.version
        item.cleaning_started()
        await to_thread(self.store.save, item, expected, actor.user_id, actor.project_id,
                        "cleaning_started")
        try:
            for node_uuid in item.node_uuids:
                await self.runtime.return_and_clean(node_uuid)
            for node_uuid in item.node_uuids:
                await self.runtime.clear_lessee(node_uuid)
        except Exception as exc:
            expected = item.version
            item.mark_error(f"return or cleaning failed: {exc}")
            await to_thread(self.store.save, item, expected, actor.user_id, actor.project_id,
                            "cleaning_failed")
            raise
        expected = item.version
        item.cleaning_completed()
        await to_thread(self.store.save, item, expected, actor.user_id, actor.project_id,
                        "returned", True)
        return item

    async def reject(self, request_id: str, actor: Actor, reason: str,
                     expected_version: int | None = None) -> AccessRequest:
        item = await to_thread(self.store.get, request_id)
        if expected_version is not None and item.version != expected_version:
            from .access_store import VersionConflict
            raise VersionConflict(f"expected version {expected_version}, got {item.version}")
        expected = item.version
        item.reject(actor, self.dcn_project_id, reason)
        await to_thread(self.store.save, item, expected, actor.user_id, actor.project_id, "rejected")
        return item

    async def cancel(self, request_id: str, actor: Actor,
                     expected_version: int | None = None) -> AccessRequest:
        item = await to_thread(self.store.get, request_id)
        if expected_version is not None and item.version != expected_version:
            from .access_store import VersionConflict
            raise VersionConflict(f"expected version {expected_version}, got {item.version}")
        expected = item.version
        item.cancel(actor)
        await to_thread(self.store.save, item, expected, actor.user_id, actor.project_id, "cancelled")
        return item

    async def expire_leases(self, actor: Actor, now: datetime | None = None) -> list[str]:
        actor.require_admin(self.dcn_project_id)
        now = now or datetime.now(timezone.utc)
        expired = [item.id for item in await to_thread(self.store.list_all) if item.expired(now)]
        completed = []
        for request_id in expired:
            await self.return_lease(request_id, actor)
            completed.append(request_id)
        return completed

    async def queue_deploy(self, request_id: str, actor: Actor, node_uuid: str,
                           image_id: str, config_drive: dict,
                           expected_version: int, idempotency_key: str | None = None) -> NodeOperation:
        item = await to_thread(self.store.get, request_id)
        self._require_lessee(item, actor, node_uuid, expected_version)
        return await self._queue_operation(
            item, actor, node_uuid, "deploy", {"image_id": image_id, "config_drive": config_drive},
            idempotency_key,
        )

    async def queue_power(self, request_id: str, actor: Actor, node_uuid: str,
                          action: str, expected_version: int,
                          idempotency_key: str | None = None) -> NodeOperation:
        item = await to_thread(self.store.get, request_id)
        self._require_lessee(item, actor, node_uuid, expected_version)
        return await self._queue_operation(
            item, actor, node_uuid, "power", {"action": action}, idempotency_key,
        )

    async def _queue_operation(self, item: AccessRequest, actor: Actor, node_uuid: str,
                               operation_type: str, payload: dict,
                               idempotency_key: str | None) -> NodeOperation:
        now = datetime.now(timezone.utc)
        operation = NodeOperation(
            id=str(uuid4()), request_id=item.id, project_id=item.project_id,
            user_id=actor.user_id, node_uuid=node_uuid, operation=operation_type,
            payload=payload, state=OperationState.QUEUED, error=None, created_at=now, updated_at=now,
        )
        persisted = await to_thread(
            self.store.create_operation_idempotent, operation, idempotency_key,
        )
        if persisted.id != operation.id:
            return persisted
        await to_thread(
            self.store.record_action, item, actor.user_id, actor.project_id,
            f"operation_queued:{operation_type}:{node_uuid}",
        )
        return persisted

    async def process_operation(self, operation_id: str) -> NodeOperation:
        operation = await to_thread(self.store.start_operation, operation_id)
        try:
            if operation.operation == "deploy":
                await self.runtime.deploy(
                    operation.node_uuid, operation.project_id, operation.payload["image_id"],
                    operation.payload["config_drive"],
                )
            elif operation.operation == "power":
                await self.runtime.set_power(
                    operation.node_uuid, operation.project_id, operation.payload["action"],
                )
            else:
                raise ValueError("unsupported queued operation")
        except Exception as exc:
            await to_thread(self.store.finish_operation, operation.id, str(exc))
            raise
        return await to_thread(self.store.finish_operation, operation.id)

    @staticmethod
    def _require_lessee(item: AccessRequest, actor: Actor, node_uuid: str,
                        expected_version: int) -> None:
        if item.version != expected_version:
            from .access_store import VersionConflict
            raise VersionConflict(f"expected version {expected_version}, got {item.version}")
        if actor.project_id != item.project_id:
            raise ValueError("lessee project is required")
        if not actor.roles.intersection({"baremetal_operator", "baremetal_admin"}):
            raise ValueError("baremetal_operator role is required")
        if item.state != RequestState.LEASED or node_uuid not in item.node_uuids:
            raise ValueError("node is not leased by this request")
