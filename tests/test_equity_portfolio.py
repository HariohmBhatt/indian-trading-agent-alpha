import os
import tempfile
import unittest
from datetime import date, timedelta

import backend.db as db


class EquityPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.tmpdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmpdir.name, "trading_agent.db")
        db.ensure_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tmpdir.cleanup()

    def test_normalize_holdings_computes_values_and_pnl(self):
        from backend.brokers.kite import normalize_holdings

        rows = normalize_holdings([
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "quantity": 10,
                "average_price": 100,
                "last_price": 125,
                "day_change": 2,
            }
        ])

        self.assertEqual(rows[0]["tradingsymbol"], "RELIANCE")
        self.assertEqual(rows[0]["invested_value"], 1000)
        self.assertEqual(rows[0]["current_value"], 1250)
        self.assertEqual(rows[0]["pnl"], 250)
        self.assertEqual(rows[0]["pnl_pct"], 25)

    def test_kite_status_hides_secrets_and_expires_old_token(self):
        from backend.brokers.kite import get_kite_status

        db.set_setting("kite_api_key", "abcd1234wxyz")
        db.set_setting("kite_api_secret", "secret1234")
        db.set_setting("kite_access_token", "token123")
        db.set_setting("kite_access_token_date", (date.today() - timedelta(days=1)).isoformat())

        status = get_kite_status()

        self.assertTrue(status["configured"])
        self.assertFalse(status["connected_today"])
        self.assertEqual(status["masked_api_key"], "abcd...wxyz")
        self.assertNotIn("secret1234", str(status))
        self.assertNotIn("token123", str(status))

    def test_review_calculates_summary_actions_and_persists(self):
        from backend.equity_portfolio import build_equity_portfolio_review, create_and_save_review

        holdings = [
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "quantity": 10,
                "average_price": 100,
                "last_price": 125,
                "invested_value": 1000,
                "current_value": 1250,
                "pnl": 250,
                "pnl_pct": 25,
                "day_change": 1,
            },
            {
                "tradingsymbol": "TCS",
                "exchange": "NSE",
                "quantity": 5,
                "average_price": 200,
                "last_price": 160,
                "invested_value": 1000,
                "current_value": 800,
                "pnl": -200,
                "pnl_pct": -20,
                "day_change": -2,
            },
        ]

        review = build_equity_portfolio_review(holdings, enrich=False)
        self.assertEqual(review["summary"]["total_current"], 2050)
        self.assertEqual(review["summary"]["total_pnl"], 50)
        self.assertEqual(review["summary"]["total_pnl_pct"], 2.5)
        self.assertEqual(review["summary"]["total_day_pnl"], 0)
        self.assertTrue(any(h["action"] == "REVIEW" for h in review["holdings"]))

        saved = create_and_save_review(holdings, enrich=False)
        latest = db.get_latest_equity_portfolio_review()
        history = db.list_equity_portfolio_reviews()

        self.assertEqual(latest["review_id"], saved["review_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["summary"]["total_current"], 2050)


if __name__ == "__main__":
    unittest.main()
