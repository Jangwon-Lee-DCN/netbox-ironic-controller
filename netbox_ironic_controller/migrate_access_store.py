import os
import sqlite3

from .access_store import AccessStore


TABLES = {
    "access_requests": ("id", "project_id", "user_id", "profile", "quantity", "purpose", "requested_until", "rack", "state", "node_uuids", "reviewer_id", "decision_reason", "version"),
    "node_reservations": ("node_uuid", "request_id", "project_id", "created_at"),
    "access_audit_events": ("id", "request_id", "actor_user_id", "actor_project_id", "action", "before_json", "after_json", "created_at"),
    "request_idempotency": ("project_id", "idempotency_key", "request_id"),
    "node_operations": ("id", "request_id", "project_id", "user_id", "node_uuid", "operation", "payload_json", "state", "error", "created_at", "updated_at"),
    "operation_idempotency": ("project_id", "operation", "idempotency_key", "operation_id"),
}


def main() -> None:
    source = sqlite3.connect(os.environ["RACKD_LEGACY_DATABASE_PATH"])
    source.row_factory = sqlite3.Row
    target = AccessStore(os.environ["RACKD_ACCESS_DATABASE_URL"])
    with target._transaction() as connection:
        if connection.execute("SELECT COUNT(*) AS count FROM access_requests").fetchone()["count"]:
            raise RuntimeError("target is not empty; refusing a repeated migration")
        for table, columns in TABLES.items():
            rows = source.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
            marks = ",".join("?" for _ in columns)
            for row in rows:
                connection.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})",
                    tuple(row[column] for column in columns),
                )
            print(f"{table}={len(rows)}")


if __name__ == "__main__":
    main()
