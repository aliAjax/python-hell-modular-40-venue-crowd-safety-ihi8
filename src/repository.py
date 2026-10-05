import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .domain import ConflictError, NotFoundError, ValidationError


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
                CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_batch_no
                    ON entities(json_extract(data, '$.batch_no'))
                    WHERE kind = 'admission_batch';
            """)

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
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

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

    def find_batch_by_no(self, batch_no):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE kind = 'admission_batch' "
                "AND json_extract(data, '$.batch_no') = ?",
                (batch_no,),
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def return_admission_batch(self, *, batch_no, gate_id, zone_id, count, admitted_at, actor_id, allowed_statuses):
        """Atomically find-or-create a pending batch, reconcile zone occupancy, and mark the batch returned.

        Runs in a single BEGIN IMMEDIATE transaction so concurrent gates serialize on the latest
        committed occupancy; a duplicate batch_no resolves to the already-returned batch (idempotent).
        Raises ConflictError if the batch was voided or the zone is not accepting admissions.
        """
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            brow = connection.execute(
                "SELECT * FROM entities WHERE kind = 'admission_batch' "
                "AND json_extract(data, '$.batch_no') = ?",
                (batch_no,),
            ).fetchone()
            if brow:
                batch = self._entity_from_row(brow)
                if batch["status"] == "returned":
                    if batch["data"].get("gate_id") != gate_id or batch["data"].get("zone_id") != zone_id:
                        raise ConflictError(
                            "batch_no %s already registered for a different gate/zone" % batch_no
                        )
                    connection.commit()
                    return {
                        "batch": batch,
                        "zone": self.get_entity(zone_id),
                        "idempotent": True,
                        "created": False,
                    }
                if batch["status"] == "void":
                    raise ConflictError("batch has been voided: " + batch_no)
                if batch["data"].get("gate_id") != gate_id or batch["data"].get("zone_id") != zone_id:
                    raise ConflictError(
                        "batch_no %s already registered for a different gate/zone" % batch_no
                    )
            else:
                batch = None
            zrow = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (zone_id,)
            ).fetchone()
            if not zrow:
                raise NotFoundError("zone not found: " + zone_id)
            zone = self._entity_from_row(zrow)
            if zone["kind"] != "zone":
                raise ValidationError("entity is not a zone: " + zone_id)
            if zone["status"] not in allowed_statuses:
                raise ConflictError(
                    "zone is not accepting admissions (status: %s)" % zone["status"]
                )
            capacity = int(zone["data"].get("capacity", 0))
            occupancy = int(zone["data"].get("current_occupancy", 0))
            new_occupancy = occupancy + int(count)
            excess = max(0, new_occupancy - capacity)
            zone_data = dict(zone["data"])
            zone_data["current_occupancy"] = new_occupancy
            zone_data["over_capacity"] = excess > 0
            connection.execute(
                "UPDATE entities SET version = version + 1, data = ?, updated_at = ? WHERE id = ?",
                (json.dumps(zone_data, ensure_ascii=False, sort_keys=True), now, zone["id"]),
            )
            batch_data = {
                "batch_no": batch_no,
                "gate_id": gate_id,
                "zone_id": zone_id,
                "count": int(count),
                "admitted_at": admitted_at,
                "exceeded_capacity": excess > 0,
                "excess_count": excess,
                "reviewed": False,
                "returned_at": now,
            }
            batch_payload = json.dumps(batch_data, ensure_ascii=False, sort_keys=True)
            if batch:
                connection.execute(
                    "UPDATE entities SET status = 'returned', version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ?",
                    (batch_payload, now, batch["id"]),
                )
                batch_id = batch["id"]
                created = False
            else:
                batch_id = str(uuid4())
                connection.execute(
                    "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                    "VALUES (?, 'admission_batch', 'returned', 1, ?, ?, ?, ?)",
                    (batch_id, batch_payload, actor_id, now, now),
                )
                created = True
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "batch": self.get_entity(batch_id),
            "zone": self.get_entity(zone_id),
            "idempotent": False,
            "created": created,
        }

    def void_pending_batches(self, zone_id):
        now = utcnow()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE entities SET status = 'void', version = version + 1, updated_at = ? "
                "WHERE kind = 'admission_batch' AND status = 'pending' "
                "AND json_extract(data, '$.zone_id') = ?",
                (now, zone_id),
            )
            return cursor.rowcount

    def backfill_zone_occupancy(self):
        """Reconcile each zone's current_occupancy from its returned admission batches.

        Used when upgrading an old database: occupancy is recomputed as the sum of counts of
        returned batches, so the on-site headcount matches the reconciled batch records.
        """
        now = utcnow()
        connection = self._connect()
        updated = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT json_extract(data, '$.zone_id') AS zone_id, "
                "SUM(CAST(json_extract(data, '$.count') AS INTEGER)) AS total "
                "FROM entities WHERE kind = 'admission_batch' AND status = 'returned' "
                "AND json_extract(data, '$.zone_id') IS NOT NULL "
                "GROUP BY zone_id"
            ).fetchall()
            for row in rows:
                zone_id = row["zone_id"]
                total = int(row["total"] or 0)
                zrow = connection.execute(
                    "SELECT * FROM entities WHERE id = ?", (zone_id,)
                ).fetchone()
                if not zrow:
                    continue
                zone = self._entity_from_row(zrow)
                data = dict(zone["data"])
                data["current_occupancy"] = total
                data["over_capacity"] = total > int(data.get("capacity", 0))
                connection.execute(
                    "UPDATE entities SET version = version + 1, data = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(data, ensure_ascii=False, sort_keys=True), now, zone_id),
                )
                updated.append({"zone_id": zone_id, "occupancy": total})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return updated

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
