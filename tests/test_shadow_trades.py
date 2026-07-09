import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import backend.db as db


class ShadowTradeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.tmpdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmpdir.name, "trading_agent.db")
        db.ensure_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tmpdir.cleanup()

    def _insert_shadow_trade(self, ticker="TEST", entry_price=100.0, **overrides):
        signal_date = (date.today() - timedelta(days=15)).isoformat()
        values = {
            "ticker": ticker,
            "signal_date": signal_date,
            "signal": "STRONG BUY",
            "score": 5.0,
            "confidence": "MEDIUM",
            "success_probability": 74,
            "entry_price": entry_price,
            "price_1d": None,
            "price_3d": None,
            "price_5d": None,
            "price_10d": None,
            "pnl_1d_pct": None,
            "pnl_3d_pct": None,
            "pnl_5d_pct": None,
            "pnl_10d_pct": None,
        }
        values.update(overrides)
        with db.get_db() as conn:
            conn.execute(
                """INSERT INTO shadow_trades
                (ticker, signal_date, signal, score, confidence, success_probability,
                 entry_price, price_1d, price_3d, price_5d, price_10d,
                 pnl_1d_pct, pnl_3d_pct, pnl_5d_pct, pnl_10d_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    values["ticker"],
                    values["signal_date"],
                    values["signal"],
                    values["score"],
                    values["confidence"],
                    values["success_probability"],
                    values["entry_price"],
                    values["price_1d"],
                    values["price_3d"],
                    values["price_5d"],
                    values["price_10d"],
                    values["pnl_1d_pct"],
                    values["pnl_3d_pct"],
                    values["pnl_5d_pct"],
                    values["pnl_10d_pct"],
                ),
            )
        return values

    def _get_shadow_trade(self, ticker="TEST"):
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        return dict(row)

    def test_refresh_shadow_prices_backfills_missing_horizons(self):
        trade = self._insert_shadow_trade()
        prices_by_days = {1: 101.0, 3: 103.0, 5: 105.0, 10: 110.0}

        def fake_price(symbol, entry_date, days):
            self.assertEqual(symbol, "TEST.NS")
            self.assertEqual(entry_date, trade["signal_date"])
            return prices_by_days[days]

        with patch("backend.simulation._price_n_days_later", side_effect=fake_price):
            from backend.shadow_trades import refresh_shadow_prices

            result = refresh_shadow_prices()

        self.assertEqual(result, {"status": "ok", "scanned": 1, "updated": 1})
        row = self._get_shadow_trade()
        self.assertEqual(row["price_1d"], 101.0)
        self.assertEqual(row["price_3d"], 103.0)
        self.assertEqual(row["price_5d"], 105.0)
        self.assertEqual(row["price_10d"], 110.0)
        self.assertEqual(row["pnl_1d_pct"], 1.0)
        self.assertEqual(row["pnl_3d_pct"], 3.0)
        self.assertEqual(row["pnl_5d_pct"], 5.0)
        self.assertEqual(row["pnl_10d_pct"], 10.0)

    def test_refresh_shadow_prices_does_not_overwrite_existing_values(self):
        trade = self._insert_shadow_trade(
            price_1d=105.0,
            pnl_1d_pct=5.0,
        )

        def fake_price(symbol, entry_date, days):
            self.assertNotEqual(days, 1)
            self.assertEqual(entry_date, trade["signal_date"])
            return {3: 103.0, 5: 105.0, 10: 110.0}[days]

        with patch("backend.simulation._price_n_days_later", side_effect=fake_price):
            from backend.shadow_trades import refresh_shadow_prices

            result = refresh_shadow_prices()

        self.assertEqual(result, {"status": "ok", "scanned": 1, "updated": 1})
        row = self._get_shadow_trade()
        self.assertEqual(row["price_1d"], 105.0)
        self.assertEqual(row["pnl_1d_pct"], 5.0)
        self.assertEqual(row["price_3d"], 103.0)
        self.assertEqual(row["pnl_3d_pct"], 3.0)

    def test_paper_trade_refresh_returns_shadow_result_when_shadow_refresh_succeeds(self):
        shadow_result = {"status": "ok", "scanned": 1, "updated": 1}
        with patch("backend.shadow_trades.refresh_shadow_prices", return_value=shadow_result):
            from backend.simulation import refresh_paper_trade_prices

            result = refresh_paper_trade_prices()

        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["total_active"], 0)
        self.assertEqual(result["shadow"], shadow_result)


if __name__ == "__main__":
    unittest.main()
