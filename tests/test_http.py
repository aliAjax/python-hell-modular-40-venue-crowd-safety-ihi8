import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HttpBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "http.db"),
            RuleEngine(),
        )
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), static_dir)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {
            "X-User-Id": "gate-op",
            "X-Role": "operator",
            "Content-Type": "application/json",
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            method,
            path,
            body=json.dumps(body) if body is not None else None,
            headers=self.headers,
        )
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def _action(self, entity_id, action, data):
        return self._request(
            "POST",
            "/api/entities/%s/actions" % entity_id,
            {"action": action, "data": data},
        )

    def test_batch_return_http_end_to_end(self):
        self.headers["X-User-Id"] = "commander"
        self.headers["X-Role"] = "coordinator"
        status, venue = self._request(
            "POST", "/api/venue", {"name": "V", "address": "A"}
        )
        self.assertEqual(status, 201)
        status, zone = self._request(
            "POST",
            "/api/zone",
            {"venue_id": venue["id"], "name": "Z", "capacity": 1000},
        )
        self.assertEqual(status, 201)
        status, gate = self._request(
            "POST",
            "/api/gate",
            {"venue_id": venue["id"], "name": "G", "zone_ids": [zone["id"]]},
        )
        self.assertEqual(status, 201)
        self._action(gate["id"], "open", {"operator_id": "gate-op"})
        self._action(zone["id"], "open", {"checklist": "clear"})

        self.headers["X-User-Id"] = "gate-op"
        self.headers["X-Role"] = "operator"
        status, batch = self._request(
            "POST",
            "/api/admission_batches/return",
            {
                "gate_id": gate["id"],
                "zone_id": zone["id"],
                "count": 600,
                "batch_no": "B-1",
                "admitted_at": "t",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(batch["status"], "returned")

        status, batch2 = self._request(
            "POST",
            "/api/admission_batches/return",
            {
                "gate_id": gate["id"],
                "zone_id": zone["id"],
                "count": 500,
                "batch_no": "B-2",
                "admitted_at": "t",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(batch2["data"]["exceeded_capacity"])
        self.assertEqual(batch2["data"]["excess_count"], 100)

        status, zone = self._request("GET", "/api/entities/%s" % zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 1100)
        self.assertTrue(zone["data"]["over_capacity"])

        status, review = self._request("GET", "/api/admission_batches/review")
        self.assertEqual(len(review["items"]), 1)
        self.assertEqual(review["items"][0]["id"], batch2["id"])

        # duplicate return is idempotent
        status, dup = self._request(
            "POST",
            "/api/admission_batches/return",
            {
                "gate_id": gate["id"],
                "zone_id": zone["id"],
                "count": 500,
                "batch_no": "B-2",
                "admitted_at": "t",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(dup["id"], batch2["id"])
        status, zone = self._request("GET", "/api/entities/%s" % zone["id"])
        self.assertEqual(zone["data"]["current_occupancy"], 1100)

        # commander reviews the over-capacity batch
        self.headers["X-User-Id"] = "commander"
        self.headers["X-Role"] = "coordinator"
        status, reviewed = self._request(
            "POST",
            "/api/admission_batches/%s/review" % batch2["id"],
            {"note": "verified"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(reviewed["data"]["reviewed"])
        status, review = self._request("GET", "/api/admission_batches/review")
        self.assertEqual(review["items"], [])


if __name__ == "__main__":
    unittest.main()
