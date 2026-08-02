import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import backend.db as db


def kite_row(symbol: str, price: float = 100) -> dict:
    return {
        "tradingsymbol": symbol,
        "exchange": "NSE",
        "quantity": 10,
        "t1_quantity": 0,
        "average_price": 90,
        "last_price": price,
        "close_price": price,
        "invested_value": 900,
        "current_value": price * 10,
        "pnl": (price - 90) * 10,
        "pnl_pct": (price - 90) / 90 * 100,
        "day_change": 1,
        "day_change_pct": 1,
        "source": "kite",
    }


class PositionsFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.tmpdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmpdir.name, "trading_agent.db")
        db.ensure_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tmpdir.cleanup()

    def test_malformed_payload_fails_and_preserves_previous_holdings(self):
        from backend.brokers.kite import KiteHoldingsError
        from backend.positions import POSITIONS_SYNC_STATUS, sync_positions_from_kite

        db.upsert_position(kite_row("OLD"))
        client = SimpleNamespace(holdings=lambda: {"data": []})

        with patch("backend.brokers.kite.get_authenticated_client", return_value=client):
            with self.assertRaises(KiteHoldingsError):
                sync_positions_from_kite()

        self.assertIsNotNone(db.get_position("OLD", "NSE"))
        self.assertIsNone(db.get_position("NEW", "NSE"))
        self.assertEqual(db.get_setting(POSITIONS_SYNC_STATUS), "failed")
        self.assertIn("list", db.get_setting("positions_sync_error"))

    def test_confirmed_empty_payload_replaces_kite_rows_but_keeps_manual_rows(self):
        from backend.positions import (
            POSITIONS_LAST_SUCCESS,
            POSITIONS_SYNC_STATUS,
            save_manual_position,
            sync_positions_from_kite,
        )

        db.upsert_position(kite_row("KITE"))
        save_manual_position({
            "tradingsymbol": "MANUAL",
            "exchange": "NSE",
            "quantity": 2,
            "average_price": 50,
            "last_price": 55,
        })

        client = SimpleNamespace(holdings=lambda: [])
        with patch("backend.brokers.kite.get_authenticated_client", return_value=client):
            result = sync_positions_from_kite()

        self.assertEqual(result["sync_status"], "empty")
        self.assertEqual(db.get_setting(POSITIONS_SYNC_STATUS), "empty")
        self.assertIsNotNone(db.get_setting(POSITIONS_LAST_SUCCESS))
        self.assertIsNone(db.get_position("KITE", "NSE"))
        self.assertEqual(db.get_position("MANUAL", "NSE")["source"], "manual")

    def test_transient_kite_error_marks_failure_without_changing_holdings(self):
        from backend.positions import POSITIONS_SYNC_STATUS, sync_positions_from_kite

        db.upsert_position(kite_row("OLD"))
        client = SimpleNamespace(holdings=lambda: (_ for _ in ()).throw(TimeoutError("broker timeout")))

        with patch("backend.brokers.kite.get_authenticated_client", return_value=client):
            with self.assertRaises(TimeoutError):
                sync_positions_from_kite()

        self.assertEqual(db.get_setting(POSITIONS_SYNC_STATUS), "failed")
        self.assertIn("timeout", db.get_setting("positions_sync_error"))
        self.assertIsNotNone(db.get_position("OLD", "NSE"))

    def test_kite_replacement_rolls_back_when_a_row_write_fails(self):
        original_upsert = db._upsert_position_conn
        calls = 0

        def fail_on_second_row(conn, row):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated write failure")
            return original_upsert(conn, row)

        db.upsert_position(kite_row("OLD"))
        with patch.object(db, "_upsert_position_conn", side_effect=fail_on_second_row):
            with self.assertRaises(RuntimeError):
                db.replace_kite_positions([kite_row("NEW1"), kite_row("NEW2")])

        self.assertIsNotNone(db.get_position("OLD", "NSE"))
        self.assertIsNone(db.get_position("NEW1", "NSE"))
        self.assertIsNone(db.get_position("NEW2", "NSE"))

    def test_stale_kite_positions_are_refused_for_review(self):
        from backend.positions import (
            POSITIONS_LAST_SUCCESS,
            POSITIONS_SYNC_STATUS,
            PositionsFreshnessError,
            get_positions_for_review,
        )

        db.upsert_position(kite_row("STALE"))
        db.set_setting(POSITIONS_SYNC_STATUS, "success")
        db.set_setting(
            POSITIONS_LAST_SUCCESS,
            (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        )

        with self.assertRaises(PositionsFreshnessError):
            get_positions_for_review()

    def test_failed_kite_sync_is_refused_but_manual_only_review_is_allowed(self):
        from backend.positions import (
            POSITIONS_SYNC_ERROR,
            POSITIONS_SYNC_STATUS,
            PositionsFreshnessError,
            get_positions_for_review,
            save_manual_position,
        )

        db.upsert_position(kite_row("FAILED"))
        db.set_setting(POSITIONS_SYNC_STATUS, "failed")
        db.set_setting(POSITIONS_SYNC_ERROR, "temporary broker response")
        with self.assertRaises(PositionsFreshnessError):
            get_positions_for_review()

        db.delete_position("FAILED")
        save_manual_position({
            "tradingsymbol": "LOCAL",
            "exchange": "NSE",
            "quantity": 1,
            "average_price": 100,
        })
        positions = get_positions_for_review()
        self.assertEqual([p["tradingsymbol"] for p in positions], ["LOCAL"])


if __name__ == "__main__":
    unittest.main()
