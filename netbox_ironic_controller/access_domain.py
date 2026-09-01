from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class RequestState(StrEnum):
    SUBMITTED = "submitted"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    ALLOCATING = "allocating"
    LEASED = "leased"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    RETURN_REQUESTED = "return_requested"
    CLEANING = "cleaning"
    RETURNED = "returned"
    ERROR = "error"


class DomainError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    user_id: str
    project_id: str
    roles: frozenset[str]

    def require_admin(self, dcn_project_id: str) -> None:
        if self.project_id != dcn_project_id or "baremetal_admin" not in self.roles:
            raise DomainError("dcn baremetal_admin role is required")


@dataclass(frozen=True)
class OfferCandidate:
    node_uuid: str
    rack: str
    profile: str
    netbox_active: bool
    offer_enabled: bool
    provision_state: str
    maintenance: bool
    lessee: str | None
    last_error: str | None

    @property
    def eligible(self) -> bool:
        return all((
            self.netbox_active,
            self.offer_enabled,
            self.provision_state == "available",
            not self.maintenance,
            not self.lessee,
            not self.last_error,
        ))


@dataclass
class AccessRequest:
    id: str
    project_id: str
    user_id: str
    profile: str
    quantity: int
    purpose: str
    requested_until: datetime
    rack: str | None = None
    state: RequestState = RequestState.SUBMITTED
    node_uuids: list[str] = field(default_factory=list)
    reviewer_id: str | None = None
    decision_reason: str | None = None
    version: int = 0

    def begin_review(self, actor: Actor, dcn_project_id: str) -> None:
        actor.require_admin(dcn_project_id)
        self._transition(RequestState.SUBMITTED, RequestState.REVIEWING)

    def approve(self, actor: Actor, dcn_project_id: str, candidates: list[OfferCandidate]) -> None:
        actor.require_admin(dcn_project_id)
        if self.state not in (RequestState.SUBMITTED, RequestState.REVIEWING):
            raise DomainError(f"request cannot be approved from {self.state}")
        eligible = [node for node in candidates if node.eligible and node.profile == self.profile]
        if self.rack:
            eligible = [node for node in eligible if node.rack == self.rack]
        if len(eligible) < self.quantity:
            raise DomainError("not enough eligible nodes")
        self.node_uuids = [node.node_uuid for node in eligible[: self.quantity]]
        self.reviewer_id = actor.user_id
        self.state = RequestState.APPROVED
        self.version += 1

    def allocation_started(self) -> None:
        self._transition(RequestState.APPROVED, RequestState.ALLOCATING)

    def allocation_completed(self) -> None:
        self._transition(RequestState.ALLOCATING, RequestState.LEASED)

    def request_return(self, actor: Actor) -> None:
        if actor.project_id != self.project_id and "baremetal_admin" not in actor.roles:
            raise DomainError("requester project or baremetal_admin is required")
        self._transition(RequestState.LEASED, RequestState.RETURN_REQUESTED)

    def cleaning_started(self) -> None:
        self._transition(RequestState.RETURN_REQUESTED, RequestState.CLEANING)

    def cleaning_completed(self) -> None:
        self._transition(RequestState.CLEANING, RequestState.RETURNED)

    def mark_error(self, reason: str) -> None:
        if self.state in (RequestState.RETURNED, RequestState.REJECTED, RequestState.CANCELLED):
            raise DomainError(f"terminal request cannot fail from {self.state}")
        self.state = RequestState.ERROR
        self.decision_reason = reason[:1000]
        self.version += 1

    def expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.state == RequestState.LEASED and self.requested_until <= now

    def _transition(self, expected: RequestState, target: RequestState) -> None:
        if self.state != expected:
            raise DomainError(f"expected {expected}, got {self.state}")
        self.state = target
        self.version += 1
