import json
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch


class BoundedExecutionTests(unittest.TestCase):
    def test_admission_rejects_when_worker_and_queue_are_full(self):
        from backend.execution import BoundedExecutor, ExecutionRejected

        executor = BoundedExecutor("phase08-saturation", max_workers=1, max_queue=0)
        started = threading.Event()
        release = threading.Event()

        def blocking_work():
            started.set()
            release.wait(timeout=2)
            return "done"

        try:
            first = executor.submit(blocking_work)
            self.assertTrue(started.wait(timeout=1))
            with self.assertRaises(ExecutionRejected):
                executor.submit(lambda: "rejected")
            release.set()
            self.assertEqual(first.result(timeout=1), "done")
        finally:
            release.set()
            executor.shutdown()

    def test_timeout_does_not_cancel_running_python_thread(self):
        from backend.execution import (
            BoundedExecutor,
            ExecutionTimeout,
            wait_for,
        )

        executor = BoundedExecutor("phase08-timeout", max_workers=1, max_queue=0)
        started = threading.Event()
        release = threading.Event()

        def blocking_work():
            started.set()
            release.wait(timeout=2)
            return "finished"

        try:
            future = executor.submit(blocking_work)
            self.assertTrue(started.wait(timeout=1))
            with self.assertRaises(ExecutionTimeout):
                wait_for(future, 0.01, workload="phase08-timeout")
            self.assertFalse(future.cancelled())
            release.set()
            self.assertEqual(future.result(timeout=1), "finished")
        finally:
            release.set()
            executor.shutdown()

    def test_job_failure_is_structured(self):
        from backend.observability import log_job_failure

        with self.assertLogs("indian_trading_agent", level="ERROR") as captured:
            log_job_failure(
                "analysis",
                job_id="job-123",
                error=RuntimeError("vendor unavailable"),
                duration_ms=12.5,
            )

        payload = json.loads(captured.records[-1].getMessage())
        self.assertEqual(payload["event"], "job_failed")
        self.assertEqual(payload["workload"], "analysis")
        self.assertEqual(payload["job_id"], "job-123")
        self.assertEqual(payload["error_type"], "RuntimeError")


class BoundedWorkerTests(unittest.TestCase):
    class ImmediateExecutor:
        def submit(self, function, *args, **kwargs):
            future = Future()
            try:
                future.set_result(function(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            return future

    def test_scanner_caps_stock_submissions(self):
        import backend.scanner as scanner
        from backend.execution import MAX_SCANNER_STOCKS

        tickers = [f"STOCK{i}" for i in range(MAX_SCANNER_STOCKS + 25)]
        with (
            patch.object(scanner, "UNIVERSES", {"test": tickers}),
            patch.object(scanner, "_fetch_stock_data", return_value=None) as fetch,
            patch.object(
                scanner,
                "get_executor",
                return_value=self.ImmediateExecutor(),
            ),
        ):
            result = scanner.run_scan(universe="test", strategies=["gap"])

        self.assertEqual(result["total_stocks"], MAX_SCANNER_STOCKS)
        self.assertEqual(result["requested_stocks"], len(tickers))
        self.assertEqual(fetch.call_count, MAX_SCANNER_STOCKS)

    def test_performance_caps_stock_submissions(self):
        import backend.performance as performance
        from backend.execution import MAX_PERFORMANCE_STOCKS

        tickers = [f"STOCK{i}" for i in range(MAX_PERFORMANCE_STOCKS + 25)]
        with (
            patch.object(performance, "UNIVERSES", {"test": tickers}),
            patch.object(performance, "_fetch_history", return_value=None) as fetch,
            patch.object(
                performance,
                "get_executor",
                return_value=self.ImmediateExecutor(),
            ),
        ):
            result = performance.measure_gap_strategy(universe="test")

        self.assertEqual(result["total_signals"], 0)
        self.assertEqual(fetch.call_count, MAX_PERFORMANCE_STOCKS)

    def test_recommender_caps_stock_submissions(self):
        import backend.recommender as recommender
        from backend.execution import MAX_RECOMMENDER_STOCKS

        tickers = [f"STOCK{i}" for i in range(MAX_RECOMMENDER_STOCKS + 25)]
        with (
            patch.object(recommender, "UNIVERSES", {"test": tickers}),
            patch.object(recommender, "_refresh_active_weights"),
            patch.object(recommender, "_analyze_stock", return_value=None) as analyze,
            patch.object(
                recommender,
                "get_executor",
                return_value=self.ImmediateExecutor(),
            ),
        ):
            result = recommender.recommend(
                universe="test",
                apply_market_bias=False,
                apply_event_filter=False,
                apply_concentration_check=False,
            )

        self.assertEqual(result["total_analyzed"], MAX_RECOMMENDER_STOCKS)
        self.assertEqual(result["requested_stocks"], len(tickers))
        self.assertEqual(analyze.call_count, MAX_RECOMMENDER_STOCKS)


class InputCapAndTimeoutTests(unittest.TestCase):
    def test_expensive_request_inputs_are_rejected_at_model_boundary(self):
        from pydantic import ValidationError

        from backend.models import AnalysisRequest
        from backend.routers.backtest import BacktestRequest
        from backend.routers.scanner import ScanRequest

        with self.assertRaises(ValidationError):
            AnalysisRequest(
                ticker="RELIANCE",
                trade_date="2026-08-01",
                analysts=["market", "social", "news", "fundamentals", "extra"],
            )
        with self.assertRaises(ValidationError):
            AnalysisRequest(
                ticker="RELIANCE",
                trade_date="2026-08-01",
                max_debate_rounds=99,
            )
        with self.assertRaises(ValidationError):
            ScanRequest(strategies=["gap", "volume", "breakout", "extra"])
        with self.assertRaises(ValidationError):
            BacktestRequest(
                ticker="RELIANCE",
                start_date="2026-01-01",
                end_date="2026-01-31",
                interval_days=99,
            )

    def test_scanner_yfinance_call_has_explicit_timeout(self):
        import backend.scanner as scanner
        from backend.execution import YFINANCE_TIMEOUT_SECONDS

        captured = {}

        class EmptyHistory:
            empty = True

            def __len__(self):
                return 0

        class FakeTicker:
            def history(self, **kwargs):
                captured.update(kwargs)
                return EmptyHistory()

        with patch.object(scanner.yf, "Ticker", return_value=FakeTicker()):
            self.assertIsNone(scanner._fetch_stock_data("RELIANCE"))

        self.assertEqual(captured["timeout"], YFINANCE_TIMEOUT_SECONDS)

    def test_backtest_date_cap_is_enforced_before_pipeline_creation(self):
        from backend.backtest_engine import run_backtest
        from backend.execution import MAX_BACKTEST_DATES, InputLimitExceeded

        with self.assertRaises(InputLimitExceeded):
            run_backtest(
                ticker="RELIANCE",
                dates=[f"2026-01-{day:02d}" for day in range(1, MAX_BACKTEST_DATES + 2)],
                initial_capital=100000,
                position_size_pct=10,
                enable_learning=False,
                config={},
            )


class RequestAndComposeObservabilityTests(unittest.TestCase):
    def test_failed_request_has_request_id_and_structured_log(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from backend.observability import RequestObservabilityMiddleware

        app = FastAPI()
        app.add_middleware(RequestObservabilityMiddleware)

        @app.get("/failure")
        def failure():
            raise HTTPException(status_code=503, detail="dependency unavailable")

        with self.assertLogs("indian_trading_agent", level="WARNING") as captured:
            response = TestClient(app).get(
                "/failure",
                headers={"X-Request-ID": "request-123"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["X-Request-ID"], "request-123")
        payload = json.loads(captured.records[-1].getMessage())
        self.assertEqual(payload["event"], "request_failed")
        self.assertEqual(payload["request_id"], "request-123")
        self.assertEqual(payload["status_code"], 503)

    def test_production_compose_has_limits_and_log_rotation_for_every_service(self):
        compose_path = Path(__file__).resolve().parents[1] / "deploy/docker-compose.prod.yml"
        compose = compose_path.read_text(encoding="utf-8")

        self.assertEqual(
            compose.count("deploy:\n      resources:\n        limits:"),
            3,
        )
        self.assertEqual(compose.count("logging:\n      driver: json-file"), 3)
        for resource in ("cpus:", "memory:", "pids:"):
            self.assertEqual(compose.count(f"          {resource}"), 3)
        self.assertEqual(compose.count("        max-size: 10m"), 3)
        self.assertEqual(compose.count('        max-file: "5"'), 3)


if __name__ == "__main__":
    unittest.main()
