import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class AdmissionBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "batches.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.coordinator = Actor("commander", "coordinator")
        self.supervisor = Actor("supervisor", "supervisor")
        self.operator = Actor("gate-operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _venue_zone_gates(self, capacity=100):
        venue = self.service.create(self.coordinator, "venue", {"name": "V", "address": "A"})
        zone = self.service.create(
            self.coordinator, "zone", {"venue_id": venue["id"], "name": "Z", "capacity": capacity}
        )
        gate_a = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "GA", "zone_ids": [zone["id"]]},
        )
        gate_b = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "GB", "zone_ids": [zone["id"]]},
        )
        self.service.transition(self.operator, gate_a["id"], "open", {"operator_id": "op"})
        self.service.transition(self.operator, gate_b["id"], "open", {"operator_id": "op"})
        self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})
        return venue, zone, gate_a, gate_b

    def _upload(self, gate, zone, batch_no, count, zone_status="open", **extra):
        data = {
            "gate_id": gate["id"],
            "zone_id": zone["id"],
            "batch_no": batch_no,
            "count": count,
            "recorded_at": "2026-10-05T08:00:00Z",
            "zone_status": zone_status,
        }
        data.update(extra)
        return self.service.upload_admission_batch(self.operator, data)

    def test_upload_applies_once_and_deduplicates(self):
        _, zone, gate_a, _ = self._venue_zone_gates()
        result = self._upload(gate_a, zone, "A-1", 40)
        self.assertFalse(result["deduplicated"])
        self.assertEqual(result["batch"]["status"], "applied")
        self.assertEqual(result["zone"]["data"]["current_occupancy"], 40)

        retry = self._upload(gate_a, zone, "A-1", 40)
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(retry["batch"]["id"], result["batch"]["id"])
        self.assertEqual(retry["zone"]["data"]["current_occupancy"], 40)

        audit = self.service.audit_log(entity_id=zone["id"])
        reconciles = [entry for entry in audit if entry["action"] == "reconcile"]
        self.assertEqual(len(reconciles), 1)
        self.assertEqual(reconciles[0]["detail"]["count"], 40)

    def test_two_gates_recompute_remaining_and_flag_overflow(self):
        _, zone, gate_a, gate_b = self._venue_zone_gates(capacity=100)
        first = self._upload(gate_a, zone, "A-1", 70)
        self.assertEqual(first["batch"]["status"], "applied")
        second = self._upload(gate_b, zone, "B-1", 50)
        self.assertEqual(second["batch"]["status"], "pending_review")
        self.assertEqual(second["batch"]["data"]["over_capacity_by"], 20)
        # overflow headcount is still counted as-is
        self.assertEqual(second["zone"]["data"]["current_occupancy"], 120)

        pending = self.service.list("admission_batches", status="pending_review")
        self.assertEqual([item["id"] for item in pending], [second["batch"]["id"]])

        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.operator, second["batch"]["id"], "review", {"reviewer_id": "op"}
            )
        reviewed = self.service.transition(
            self.coordinator, second["batch"]["id"], "review", {"reviewer_id": "commander"}
        )
        self.assertEqual(reviewed["status"], "reviewed")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.coordinator, second["batch"]["id"], "review", {"reviewer_id": "commander"}
            )

    def test_zone_status_change_voids_pending_batch(self):
        _, zone, gate_a, _ = self._venue_zone_gates()
        self.service.transition(self.supervisor, zone["id"], "evacuate", {"reason": "smoke"})
        result = self._upload(gate_a, zone, "A-1", 30, zone_status="open")
        self.assertEqual(result["batch"]["status"], "void")
        self.assertEqual(result["zone"]["data"]["current_occupancy"], 0)

    def test_zone_generation_mismatch_voids_batch(self):
        _, zone, gate_a, _ = self._venue_zone_gates()
        generation = self.service.get(zone["id"])["data"]["state_generation"]
        self.service.transition(self.supervisor, zone["id"], "restrict", {"reason": "crowd", "admit_limit": 80})
        self.service.transition(self.supervisor, zone["id"], "recover", {"checklist": "clear"})
        # zone is "open" again but its state moved on while the gate was offline
        stale = self._upload(gate_a, zone, "A-1", 10, zone_generation=generation)
        self.assertEqual(stale["batch"]["status"], "void")
        current = self.service.get(zone["id"])["data"]["state_generation"]
        fresh = self._upload(gate_a, zone, "A-2", 10, zone_generation=current)
        self.assertEqual(fresh["batch"]["status"], "applied")
        self.assertEqual(fresh["zone"]["data"]["current_occupancy"], 10)

    def test_failed_upload_can_be_retried_without_double_counting(self):
        _, zone, gate_a, _ = self._venue_zone_gates()
        with self.assertRaises(ValidationError):
            self._upload(gate_a, zone, "A-1", 0)
        result = self._upload(gate_a, zone, "A-1", 25)
        self.assertEqual(result["batch"]["status"], "applied")
        retry = self._upload(gate_a, zone, "A-1", 25)
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(retry["zone"]["data"]["current_occupancy"], 25)

    def test_upload_validation(self):
        _, zone, gate_a, _ = self._venue_zone_gates()
        with self.assertRaises(PermissionDenied):
            self.service.upload_admission_batch(
                Actor("viewer", "viewer"),
                {
                    "gate_id": gate_a["id"],
                    "zone_id": zone["id"],
                    "batch_no": "A-9",
                    "count": 5,
                    "recorded_at": "t1",
                    "zone_status": "open",
                },
            )
        with self.assertRaises(ValidationError):
            self._upload(gate_a, zone, "A-9", 5, zone_status="melting")
        other_venue = self.service.create(
            self.coordinator, "venue", {"name": "V2", "address": "B"}
        )
        other_zone = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": other_venue["id"], "name": "Z2", "capacity": 50},
        )
        stranger = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": other_venue["id"], "name": "GX", "zone_ids": [other_zone["id"]]},
        )
        with self.assertRaises(ValidationError):
            self._upload(stranger, zone, "X-1", 5)

    def test_migration_backfills_zone_occupancy(self):
        old_db = Path(self.tmp.name) / "old.db"
        service = DomainService(SQLiteRepository(old_db), RuleEngine())
        venue = service.create(self.coordinator, "venue", {"name": "V", "address": "A"})
        zone = service.create(
            self.coordinator, "zone", {"venue_id": venue["id"], "name": "Z", "capacity": 100}
        )

        def insert_batch(batch_id, status, count):
            payload = json.dumps(
                {
                    "gate_id": "gate-1",
                    "zone_id": zone["id"],
                    "batch_no": batch_id,
                    "count": count,
                    "recorded_at": "t0",
                    "zone_status": "open",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'admission_batch', ?, 1, ?, 'op', 't0', 't0')",
                (batch_id, status, payload),
            )

        connection = sqlite3.connect(str(old_db))
        insert_batch("b1", "applied", 30)
        insert_batch("b2", "pending_review", 15)
        insert_batch("b3", "reviewed", 5)
        insert_batch("b4", "void", 999)
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.close()

        upgraded = SQLiteRepository(old_db)
        migrated = upgraded.get_entity(zone["id"])
        self.assertEqual(migrated["data"]["current_occupancy"], 50)


if __name__ == "__main__":
    unittest.main()
