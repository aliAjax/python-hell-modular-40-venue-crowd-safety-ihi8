import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class BatchReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "batches.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("commander", "coordinator")
        self.operator = Actor("gate-op", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _venue_zone_gate(self, capacity=1000):
        venue = self.service.create(
            self.coordinator, "venue", {"name": "V", "address": "A"}
        )
        zone = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": "Z", "capacity": capacity},
        )
        gate = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "G", "zone_ids": [zone["id"]]},
        )
        self.service.transition(self.operator, gate["id"], "open", {"operator_id": "gate-op"})
        self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})
        return venue, zone, gate

    def _batch(self, gate, zone, count, batch_no, admitted_at="2026-10-05T08:00:00Z"):
        return {
            "gate_id": gate["id"],
            "zone_id": zone["id"],
            "count": count,
            "batch_no": batch_no,
            "admitted_at": admitted_at,
        }

    def test_return_batch_increments_occupancy(self):
        venue, zone, gate = self._venue_zone_gate(capacity=1000)
        batch = self.service.return_batch(self.operator, self._batch(gate, zone, 420, "B-001"))
        self.assertEqual(batch["status"], "returned")
        self.assertEqual(batch["data"]["count"], 420)
        zone = self.service.get(zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 420)
        self.assertFalse(zone["data"]["over_capacity"])

    def test_duplicate_batch_no_counts_once(self):
        venue, zone, gate = self._venue_zone_gate(capacity=1000)
        data = self._batch(gate, zone, 420, "B-001")
        first = self.service.return_batch(self.operator, data)
        second = self.service.return_batch(self.operator, data)
        self.assertEqual(first["id"], second["id"])
        zone = self.service.get(zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 420)

    def test_over_capacity_counted_and_flagged_for_review(self):
        venue, zone, gate = self._venue_zone_gate(capacity=1000)
        self.service.return_batch(self.operator, self._batch(gate, zone, 600, "B-1"))
        batch = self.service.return_batch(self.operator, self._batch(gate, zone, 500, "B-2"))
        zone = self.service.get(zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 1100)
        self.assertTrue(zone["data"]["over_capacity"])
        self.assertTrue(batch["data"]["exceeded_capacity"])
        self.assertEqual(batch["data"]["excess_count"], 100)
        review = self.service.list_review_batches()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["id"], batch["id"])

    def test_concurrent_returns_no_lost_updates(self):
        venue, zone, gate = self._venue_zone_gate(capacity=10000)
        errors = []

        def do_return(i):
            try:
                self.service.return_batch(
                    self.operator, self._batch(gate, zone, 100, "B-%d" % i)
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_return, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        zone = self.service.get(zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 2000)

    def test_zone_status_change_voids_pending_batches(self):
        venue, zone, gate = self._venue_zone_gate(capacity=1000)
        pending = self.service.create(
            self.operator,
            "admission_batch",
            self._batch(gate, zone, 300, "B-PENDING"),
        )
        self.assertEqual(pending["status"], "pending")
        self.service.transition(
            self.coordinator, zone["id"], "evacuate", {"reason": "drill"}
        )
        pending = self.service.get(pending["id"])
        self.assertEqual(pending["status"], "void")
        zone = self.service.get(zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 0)
        with self.assertRaises(ConflictError):
            self.service.return_batch(
                self.operator, self._batch(gate, zone, 300, "B-PENDING")
            )

    def test_retry_after_failure_no_double_count(self):
        venue, zone, gate = self._venue_zone_gate(capacity=1000)
        self.service.return_batch(self.operator, self._batch(gate, zone, 400, "B-1"))
        self.service.transition(
            self.coordinator, zone["id"], "evacuate", {"reason": "drill"}
        )
        with self.assertRaises(ConflictError):
            self.service.return_batch(self.operator, self._batch(gate, zone, 200, "B-2"))
        zone = self.service.get(zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 400)
        self.service.transition(
            self.coordinator, zone["id"], "recover", {"checklist": "clear"}
        )
        self.service.return_batch(self.operator, self._batch(gate, zone, 200, "B-2"))
        zone = self.service.get(zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 600)

    def test_backfill_occupancy_from_returned_batches(self):
        venue, zone, gate = self._venue_zone_gate(capacity=1000)
        self.service.return_batch(self.operator, self._batch(gate, zone, 400, "B-1"))
        self.service.return_batch(self.operator, self._batch(gate, zone, 300, "B-2"))
        zone = self.service.get(zone["id"])
        zdata = dict(zone["data"])
        zdata["current_occupancy"] = 999
        self.service.repository.update_entity(
            zone["id"], zone["version"], zone["status"], zdata
        )
        zone = self.service.get(zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 999)
        updated = self.service.backfill_occupancy(self.coordinator)
        zone = self.service.get(zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 700)
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0]["occupancy"], 700)

    def test_batch_no_reused_with_different_gate_zone_rejected(self):
        venue, zone, gate = self._venue_zone_gate(capacity=1000)
        zone2 = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": "Z2", "capacity": 1000},
        )
        gate2 = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "G2", "zone_ids": [zone2["id"]]},
        )
        self.service.transition(self.operator, gate2["id"], "open", {"operator_id": "gate-op"})
        self.service.transition(self.operator, zone2["id"], "open", {"checklist": "clear"})
        self.service.return_batch(self.operator, self._batch(gate, zone, 100, "B-1"))
        with self.assertRaises(ConflictError):
            self.service.return_batch(self.operator, self._batch(gate2, zone2, 100, "B-1"))

    def test_review_over_capacity_batch(self):
        venue, zone, gate = self._venue_zone_gate(capacity=1000)
        self.service.return_batch(self.operator, self._batch(gate, zone, 600, "B-1"))
        batch = self.service.return_batch(self.operator, self._batch(gate, zone, 500, "B-2"))
        self.assertTrue(batch["data"]["exceeded_capacity"])
        self.assertEqual(len(self.service.list_review_batches()), 1)
        reviewed = self.service.review_batch(self.coordinator, batch["id"], {"note": "ok"})
        self.assertTrue(reviewed["data"]["reviewed"])
        self.assertEqual(reviewed["data"]["reviewed_by"], "commander")
        self.assertEqual(self.service.list_review_batches(), [])
        zone = self.service.get(zone["id"])
        self.assertFalse(zone["data"]["over_capacity"])
        with self.assertRaises(ConflictError):
            self.service.review_batch(self.coordinator, batch["id"], {})


if __name__ == "__main__":
    unittest.main()
