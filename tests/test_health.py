import os
import json
import tempfile
import unittest
from unittest.mock import patch

import backend.db as db


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.tmpdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmpdir.name, "trading_agent.db")
        db.ensure_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tmpdir.cleanup()

    def test_liveness_does_not_require_database(self):
        from backend.app import health

        with patch("backend.app.db.check_db", side_effect=AssertionError("database was touched")):
            response = health()

        self.assertEqual(response["status"], "ok")
        self.assertNotIn("ready", response)

    def test_readiness_reports_database_success(self):
        from backend.app import readiness

        response = readiness()

        self.assertEqual(response["status"], "ready")
        self.assertTrue(response["ready"])
        self.assertEqual(response["checks"]["database"]["status"], "ok")

    def test_readiness_returns_service_unavailable_when_database_fails(self):
        from backend.app import readiness

        with patch("backend.app.db.check_db", side_effect=OSError("database unavailable")):
            response = readiness()

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["checks"]["database"]["status"], "failed")
        self.assertNotIn("database unavailable", response.body.decode())


if __name__ == "__main__":
    unittest.main()
