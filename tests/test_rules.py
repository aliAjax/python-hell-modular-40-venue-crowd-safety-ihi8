import unittest

from src.domain import ValidationError
from src.rules import assess_admission_batch, capacity_available, incident_priority


class RulesTest(unittest.TestCase):
    def test_capacity_boundary(self):
        self.assertTrue(capacity_available(1000, 800, 200))
        self.assertFalse(capacity_available(1000, 800, 201))

    def test_incident_priority_rank(self):
        self.assertGreater(incident_priority("critical", "fire"), incident_priority("medium", "security"))
        with self.assertRaises(ValidationError):
            incident_priority("unknown", "medical")

    def test_assess_admission_batch(self):
        zone = {
            "status": "open",
            "data": {"current_occupancy": 90, "capacity": 100, "state_generation": 2},
        }
        status, extra, patch = assess_admission_batch(zone, 5, "open", 2)
        self.assertEqual(status, "applied")
        self.assertEqual(patch["current_occupancy"], 95)

        status, extra, patch = assess_admission_batch(zone, 15, "open", 2)
        self.assertEqual(status, "pending_review")
        self.assertEqual(extra["over_capacity_by"], 5)
        self.assertEqual(patch["current_occupancy"], 105)

        status, extra, patch = assess_admission_batch(zone, 5, "closed", 2)
        self.assertEqual(status, "void")
        self.assertEqual(patch, {})

        status, extra, patch = assess_admission_batch(zone, 5, "open", 1)
        self.assertEqual(status, "void")
        self.assertEqual(patch, {})


if __name__ == "__main__":
    unittest.main()
