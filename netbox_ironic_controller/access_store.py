from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pymysql

from .access_domain import AccessRequest, DomainError, RequestState
from .access_operations import NodeOperation, OperationState


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
CREATE TABLE IF NOT EXISTS request_idempotency (
  project_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_id TEXT NOT NULL REFERENCES access_requests(id),
  PRIMARY KEY(project_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS node_operations (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL REFERENCES access_requests(id),
  project_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  node_uuid TEXT NOT NULL,
  operation TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operation_idempotency (
  project_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  operation_id TEXT NOT NULL REFERENCES node_operations(id),
  PRIMARY KEY(project_id, operation, idempotency_key)
);
"""

MYSQL_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS access_requests (
      id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL, user_id VARCHAR(64) NOT NULL,
      profile VARCHAR(255) NOT NULL, quantity INTEGER NOT NULL, purpose TEXT NOT NULL,
      requested_until VARCHAR(64) NOT NULL, rack VARCHAR(255), state VARCHAR(32) NOT NULL,
      node_uuids TEXT NOT NULL, reviewer_id VARCHAR(64), decision_reason TEXT,
      version INTEGER NOT NULL DEFAULT 0) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS node_reservations (
      node_uuid VARCHAR(64) PRIMARY KEY, request_id VARCHAR(64) NOT NULL,
      project_id VARCHAR(64) NOT NULL, created_at VARCHAR(64) NOT NULL,
      CONSTRAINT fk_reservation_request FOREIGN KEY(request_id) REFERENCES access_requests(id)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS access_audit_events (
      id VARCHAR(64) PRIMARY KEY, request_id VARCHAR(64) NOT NULL, actor_user_id VARCHAR(64) NOT NULL,
      actor_project_id VARCHAR(64) NOT NULL, action VARCHAR(64) NOT NULL, before_json TEXT NOT NULL,
      after_json TEXT NOT NULL, created_at VARCHAR(64) NOT NULL,
      INDEX idx_audit_request(request_id)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS request_idempotency (
      project_id VARCHAR(64) NOT NULL, idempotency_key VARCHAR(128) NOT NULL,
      request_id VARCHAR(64) NOT NULL, PRIMARY KEY(project_id,idempotency_key),
      CONSTRAINT fk_request_idempotency FOREIGN KEY(request_id) REFERENCES access_requests(id)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS node_operations (
      id VARCHAR(64) PRIMARY KEY, request_id VARCHAR(64) NOT NULL, project_id VARCHAR(64) NOT NULL,
      user_id VARCHAR(64) NOT NULL, node_uuid VARCHAR(64) NOT NULL, operation VARCHAR(32) NOT NULL,
      payload_json TEXT NOT NULL, state VARCHAR(32) NOT NULL, error TEXT,
      created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
      INDEX idx_operation_request(request_id), INDEX idx_operation_node(node_uuid,state),
      CONSTRAINT fk_operation_request FOREIGN KEY(request_id) REFERENCES access_requests(id)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS operation_idempotency (
      project_id VARCHAR(64) NOT NULL, operation VARCHAR(32) NOT NULL,
      idempotency_key VARCHAR(128) NOT NULL, operation_id VARCHAR(64) NOT NULL,
      PRIMARY KEY(project_id,operation,idempotency_key),
      CONSTRAINT fk_operation_idempotency FOREIGN KEY(operation_id) REFERENCES node_operations(id)) ENGINE=InnoDB""",
)


class VersionConflict(DomainError):
    pass


class NodeAlreadyReserved(DomainError):
    pass


class AccessStore:
    """Transactional store using SQLite for development and MariaDB for production."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.mysql = self.path.startswith(("mysql://", "mariadb://"))
        self.mysql_config = self._mysql_config(self.path) if self.mysql else None
        with self._connect() as connection:
            if self.mysql:
                for statement in MYSQL_SCHEMA:
                    connection.execute(statement)
                connection.commit()
            else:
                connection.executescript(SCHEMA)
                connection.execute(
                    """UPDATE node_operations SET state='failed', error=?, updated_at=?
                    WHERE state IN ('queued','running')""",
                    (
                        "service restarted before operation completion; operator reconciliation required",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

    def create(self, request: AccessRequest) -> None:
        self.create_idempotent(request, None)

    def create_idempotent(self, request: AccessRequest, idempotency_key: str | None) -> AccessRequest:
        with self._transaction() as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT request_id FROM request_idempotency WHERE project_id=? AND idempotency_key=?",
                    (request.project_id, idempotency_key),
                ).fetchone()
                if existing:
                    row = connection.execute(
                        "SELECT * FROM access_requests WHERE id=?", (existing["request_id"],),
                    ).fetchone()
                    return self._from_row(row)
            connection.execute(
                """INSERT INTO access_requests
                (id,project_id,user_id,profile,quantity,purpose,requested_until,rack,state,node_uuids,
                 reviewer_id,decision_reason,version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                self._values(request),
            )
            self._audit(
                connection, None, request, request.user_id, request.project_id, "created",
            )
            if idempotency_key:
                connection.execute(
                    "INSERT INTO request_idempotency(project_id,idempotency_key,request_id) VALUES(?,?,?)",
                    (request.project_id, idempotency_key, request.id),
                )
            return request

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

    def ping(self) -> None:
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()

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
            except (sqlite3.IntegrityError, pymysql.IntegrityError) as exc:
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

    def record_action(self, request: AccessRequest, actor_user_id: str,
                      actor_project_id: str, action: str) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM access_requests WHERE id=?", (request.id,),
            ).fetchone()
            if row is None:
                raise KeyError(request.id)
            current = self._from_row(row)
            if current.version != request.version:
                raise VersionConflict("request was changed concurrently")
            self._audit(connection, current, current, actor_user_id, actor_project_id, action)

    def create_operation(self, operation: NodeOperation) -> None:
        self.create_operation_idempotent(operation, None)

    def create_operation_idempotent(self, operation: NodeOperation,
                                    idempotency_key: str | None) -> NodeOperation:
        with self._transaction() as connection:
            if idempotency_key:
                existing = connection.execute(
                    """SELECT operation_id FROM operation_idempotency
                    WHERE project_id=? AND operation=? AND idempotency_key=?""",
                    (operation.project_id, operation.operation, idempotency_key),
                ).fetchone()
                if existing:
                    row = connection.execute(
                        "SELECT * FROM node_operations WHERE id=?", (existing["operation_id"],),
                    ).fetchone()
                    current = self._operation_from_row(row)
                    if (current.request_id, current.node_uuid, current.payload) != (
                        operation.request_id, operation.node_uuid, operation.payload,
                    ):
                        raise DomainError("Idempotency-Key was reused with a different operation")
                    return current
            active = connection.execute(
                "SELECT id FROM node_operations WHERE node_uuid=? AND state IN ('queued','running')",
                (operation.node_uuid,),
            ).fetchone()
            if active:
                raise DomainError("node already has an active operation")
            connection.execute(
                "INSERT INTO node_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (operation.id, operation.request_id, operation.project_id, operation.user_id,
                 operation.node_uuid, operation.operation, json.dumps(operation.payload, sort_keys=True),
                 operation.state, operation.error, operation.created_at.isoformat(),
                 operation.updated_at.isoformat()),
            )
            if idempotency_key:
                connection.execute(
                    "INSERT INTO operation_idempotency VALUES(?,?,?,?)",
                    (operation.project_id, operation.operation, idempotency_key, operation.id),
                )
        return operation

    def get_operation(self, operation_id: str) -> NodeOperation:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM node_operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return self._operation_from_row(row)

    def list_operations(self, request_id: str, project_id: str | None = None) -> list[NodeOperation]:
        query, values = "SELECT * FROM node_operations WHERE request_id=?", [request_id]
        if project_id is not None:
            query += " AND project_id=?"
            values.append(project_id)
        query += " ORDER BY created_at"
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._operation_from_row(row) for row in rows]

    def has_active_operations(self, request_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM node_operations
                WHERE request_id=? AND state IN ('queued','running') LIMIT 1""",
                (request_id,),
            ).fetchone()
        return row is not None

    def start_operation(self, operation_id: str) -> NodeOperation:
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE node_operations SET state='running',updated_at=? WHERE id=? AND state='queued'",
                (datetime.now(timezone.utc).isoformat(), operation_id),
            )
            if changed.rowcount != 1:
                raise VersionConflict("operation is no longer queued")
            row = connection.execute("SELECT * FROM node_operations WHERE id=?", (operation_id,)).fetchone()
        return self._operation_from_row(row)

    def finish_operation(self, operation_id: str, error: str | None = None) -> NodeOperation:
        state = OperationState.FAILED if error else OperationState.SUCCEEDED
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE node_operations SET state=?,error=?,updated_at=? WHERE id=? AND state='running'",
                (state, error[:1000] if error else None, datetime.now(timezone.utc).isoformat(), operation_id),
            )
            if changed.rowcount != 1:
                raise VersionConflict("operation is no longer running")
            row = connection.execute("SELECT * FROM node_operations WHERE id=?", (operation_id,)).fetchone()
        return self._operation_from_row(row)

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> NodeOperation:
        return NodeOperation(
            id=row["id"], request_id=row["request_id"], project_id=row["project_id"],
            user_id=row["user_id"], node_uuid=row["node_uuid"], operation=row["operation"],
            payload=json.loads(row["payload_json"]), state=OperationState(row["state"]), error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.begin() if self.mysql else connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self):
        if self.mysql:
            return _MySQLConnection(self.mysql_config)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _mysql_config(url: str) -> dict:
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
            raise ValueError("MariaDB URL requires host, username and database")
        return {
            "host": parsed.hostname,
            "port": parsed.port or 3306,
            "user": unquote(parsed.username),
            "password": unquote(parsed.password or ""),
            "database": parsed.path.strip("/"),
            "connect_timeout": 10,
            "read_timeout": 30,
            "write_timeout": 30,
            "cursorclass": pymysql.cursors.DictCursor,
            "autocommit": False,
        }

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
    def _audit(connection: sqlite3.Connection, before: AccessRequest | None, after: AccessRequest,
               actor_user_id: str, actor_project_id: str, action: str) -> None:
        connection.execute(
            "INSERT INTO access_audit_events VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid4()), after.id, actor_user_id, actor_project_id, action,
             json.dumps(AccessStore._public_state(before), sort_keys=True),
             json.dumps(AccessStore._public_state(after), sort_keys=True),
             datetime.now(timezone.utc).isoformat()),
        )

    @staticmethod
    def _public_state(request: AccessRequest | None) -> dict:
        if request is None:
            return {}
        return {"state": str(request.state), "version": request.version,
                "node_uuids": request.node_uuids, "project_id": request.project_id}


class _MySQLConnection:
    """Expose the sqlite-like connection surface used by AccessStore."""

    def __init__(self, config: dict):
        self.connection = pymysql.connect(**config)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def execute(self, statement: str, parameters=()):
        cursor = self.connection.cursor()
        cursor.execute(statement.replace("?", "%s"), parameters)
        return cursor

    def begin(self):
        self.connection.begin()

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()
