import json
import sqlite3
from datetime import datetime, timezone

from .domain import ADMISSION_BATCH_COUNTED_STATUSES, ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)
            self._migrate(connection)

    def _migrate(self, connection):
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version >= 1:
            return
        # Upgrade from a pre-reconciliation database: backfill zone
        # occupancy from the admission batches already uploaded.
        self._backfill_zone_occupancy(connection)
        connection.execute("PRAGMA user_version = 1")

    def _backfill_zone_occupancy(self, connection):
        rows = connection.execute(
            "SELECT * FROM entities WHERE kind = 'admission_batch'"
        ).fetchall()
        totals = {}
        for row in rows:
            batch = self._entity_from_row(row)
            if batch["status"] not in ADMISSION_BATCH_COUNTED_STATUSES:
                continue
            zone_id = batch["data"].get("zone_id")
            if zone_id:
                totals[zone_id] = totals.get(zone_id, 0) + int(batch["data"].get("count", 0))
        now = utcnow()
        for zone_id, occupancy in totals.items():
            zone = self._fetch_entity(connection, zone_id)
            if not zone or zone["kind"] != "zone":
                continue
            data = dict(zone["data"])
            data["current_occupancy"] = occupancy
            connection.execute(
                "UPDATE entities SET data = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False, sort_keys=True), now, zone_id),
            )

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            return self._fetch_entity(connection, entity_id)

    def _fetch_entity(self, connection, entity_id):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def apply_admission_batch(self, batch_id, batch_data, decide, actor_id):
        """Store an offline admission batch and reconcile its zone atomically.

        ``decide(zone)`` is called inside the write transaction with the
        zone's latest state and returns
        ``(batch_status, extra_batch_data, zone_patch)``. Returns
        ``(batch, zone, deduplicated)``; when a batch with the same
        (gate_id, batch_no) was already recorded it is returned unchanged
        and the zone is left untouched, so retries never double-count.
        """
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            gate_id = batch_data.get("gate_id")
            batch_no = batch_data.get("batch_no")
            rows = connection.execute(
                "SELECT * FROM entities WHERE kind = 'admission_batch'"
            ).fetchall()
            for row in rows:
                existing = self._entity_from_row(row)
                if existing["data"].get("gate_id") == gate_id and str(
                    existing["data"].get("batch_no")
                ) == str(batch_no):
                    zone = self._fetch_entity(connection, existing["data"].get("zone_id"))
                    connection.commit()
                    return existing, zone, True
            zone = self._fetch_entity(connection, batch_data.get("zone_id"))
            if not zone:
                raise NotFoundError("zone not found: " + str(batch_data.get("zone_id")))
            batch_status, extra, zone_patch = decide(zone)
            payload = dict(batch_data)
            if extra:
                payload.update(extra)
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'admission_batch', ?, 1, ?, ?, ?, ?)",
                (
                    batch_id,
                    batch_status,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    actor_id,
                    now,
                    now,
                ),
            )
            if zone_patch:
                merged = dict(zone["data"])
                merged.update(zone_patch)
                connection.execute(
                    "UPDATE entities SET version = version + 1, data = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(merged, ensure_ascii=False, sort_keys=True), now, zone["id"]),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return (
            self.get_entity(batch_id),
            self.get_entity(batch_data.get("zone_id")),
            False,
        )

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
