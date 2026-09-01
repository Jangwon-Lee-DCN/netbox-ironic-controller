from __future__ import annotations

from asyncio import to_thread
from datetime import datetime, timezone
from typing import Protocol

from .access_domain import AccessRequest, Actor, OfferCandidate, RequestState
from .access_store import AccessStore


class Inventory(Protocol):
    async def candidates(self, profile: str, rack: str | None) -> list[OfferCandidate]: ...


class IronicLeaseRuntime(Protocol):
    async def assign_lessee(self, node_uuid: str, project_id: str) -> None: ...
    async def return_and_clean(self, node_uuid: str) -> None: ...
    async def clear_lessee(self, node_uuid: str) -> None: ...


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
        if expected_version is not None and item.version != expected_version:
            from .access_store import VersionConflict
            raise VersionConflict(f"expected version {expected_version}, got {item.version}")
        expected = item.version
        item.request_return(actor)
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

    async def expire_leases(self, actor: Actor, now: datetime | None = None) -> list[str]:
        actor.require_admin(self.dcn_project_id)
        now = now or datetime.now(timezone.utc)
        expired = [item.id for item in await to_thread(self.store.list_all) if item.expired(now)]
        completed = []
        for request_id in expired:
            await self.return_lease(request_id, actor)
            completed.append(request_id)
        return completed
