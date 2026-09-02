from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class NodeOperation:
    id: str
    request_id: str
    project_id: str
    user_id: str
    node_uuid: str
    operation: str
    payload: dict
    state: OperationState
    error: str | None
    created_at: datetime
    updated_at: datetime
