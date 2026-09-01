from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .access_domain import AccessRequest, DomainError, RequestState


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS access_requests (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  profile TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  purpose TEXT NOT NULL,
  requested_until TEXT NOT NULL,
  rack TEXT,
  state TEXT NOT NULL,
  node_uuids TEXT NOT NULL DEFAULT '[]',
  reviewer_id TEXT,
  decision_reason TEXT,
  version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS node_reservations (
  node_uuid TEXT PRIMARY KEY,
  request_id TEXT NOT NULL REFERENCES access_requests(id),
  project_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_audit_events (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_project_id TEXT NOT NULL,
  action TEXT NOT NULL,
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class VersionConflict(DomainError):
    pass


class NodeAlreadyReserved(DomainError):
    pass


class AccessStore:
    """Small transactional store; SQLite in development, PostgreSQL replaces this adapter in production."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def create(self, request: AccessRequest) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO access_requests
                (id,project_id,user_id,profile,quantity,purpose,requested_until,rack,state,node_uuids,
                 reviewer_id,decision_reason,version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                self._values(request),
            )

    def get(self, request_id: str) -> AccessRequest:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM access_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise KeyError(request_id)
        return self._from_row(row)

    def list_for_project(self, project_id: str) -> list[AccessRequest]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM access_requests WHERE project_id=? ORDER BY id", (project_id,)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_all(self) -> list[AccessRequest]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM access_requests ORDER BY id").fetchall()
        return [self._from_row(row) for row in rows]

    def save_with_reservations(self, request: AccessRequest, expected_version: int,
                               actor_user_id: str, actor_project_id: str,
                               action: str = "approve") -> None:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM access_requests WHERE id=?", (request.id,)).fetchone()
            if row is None:
                raise KeyError(request.id)
            before = self._from_row(row)
            if before.version != expected_version:
                raise VersionConflict(f"expected version {expected_version}, got {before.version}")
            try:
                for node_uuid in request.node_uuids:
                    connection.execute(
                        "INSERT INTO node_reservations(node_uuid,request_id,project_id,created_at) VALUES(?,?,?,?)",
                        (node_uuid, request.id, request.project_id, datetime.now(timezone.utc).isoformat()),
                    )
            except sqlite3.IntegrityError as exc:
                raise NodeAlreadyReserved("one or more nodes are already reserved") from exc
            cursor = connection.execute(
                """UPDATE access_requests SET state=?,node_uuids=?,reviewer_id=?,decision_reason=?,version=?
                WHERE id=? AND version=?""",
                (request.state, json.dumps(request.node_uuids), request.reviewer_id,
                 request.decision_reason, request.version, request.id, expected_version),
            )
            if cursor.rowcount != 1:
                raise VersionConflict("request was changed concurrently")
            self._audit(connection, before, request, actor_user_id, actor_project_id, action)

    def save(self, request: AccessRequest, expected_version: int,
             actor_user_id: str, actor_project_id: str, action: str,
             release_reservations: bool = False) -> None:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM access_requests WHERE id=?", (request.id,)).fetchone()
            if row is None:
                raise KeyError(request.id)
            before = self._from_row(row)
            if before.version != expected_version:
                raise VersionConflict(f"expected version {expected_version}, got {before.version}")
            values = self._values(request)
            cursor = connection.execute(
                """UPDATE access_requests SET project_id=?,user_id=?,profile=?,quantity=?,purpose=?,
                requested_until=?,rack=?,state=?,node_uuids=?,reviewer_id=?,decision_reason=?,version=?
                WHERE id=? AND version=?""",
                values[1:] + (request.id, expected_version),
            )
            if cursor.rowcount != 1:
                raise VersionConflict("request was changed concurrently")
            if release_reservations:
                connection.execute("DELETE FROM node_reservations WHERE request_id=?", (request.id,))
            self._audit(connection, before, request, actor_user_id, actor_project_id, action)

    def audit_events(self, request_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM access_audit_events WHERE request_id=? ORDER BY created_at", (request_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _values(request: AccessRequest) -> tuple:
        return (
            request.id, request.project_id, request.user_id, request.profile, request.quantity,
            request.purpose, request.requested_until.isoformat(), request.rack, request.state,
            json.dumps(request.node_uuids), request.reviewer_id, request.decision_reason, request.version,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AccessRequest:
        return AccessRequest(
            id=row["id"], project_id=row["project_id"], user_id=row["user_id"],
            profile=row["profile"], quantity=row["quantity"], purpose=row["purpose"],
            requested_until=datetime.fromisoformat(row["requested_until"]), rack=row["rack"],
            state=RequestState(row["state"]), node_uuids=json.loads(row["node_uuids"]),
            reviewer_id=row["reviewer_id"], decision_reason=row["decision_reason"], version=row["version"],
        )

    @staticmethod
    def _audit(connection: sqlite3.Connection, before: AccessRequest, after: AccessRequest,
               actor_user_id: str, actor_project_id: str, action: str) -> None:
        connection.execute(
            "INSERT INTO access_audit_events VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid4()), after.id, actor_user_id, actor_project_id, action,
             json.dumps(AccessStore._public_state(before), sort_keys=True),
             json.dumps(AccessStore._public_state(after), sort_keys=True),
             datetime.now(timezone.utc).isoformat()),
        )

    @staticmethod
    def _public_state(request: AccessRequest) -> dict:
        return {"state": str(request.state), "version": request.version,
                "node_uuids": request.node_uuids, "project_id": request.project_id}
