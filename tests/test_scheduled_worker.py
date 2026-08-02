import os
import tempfile
import threading
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import backend.db as db
import backend.jobs.services as job_services
from backend.jobs.definitions import JobSpec, build_job_specs
from backend.jobs.interfaces import PortfolioSnapshot
from backend.jobs.schedules import LocalSchedule
from backend.jobs.services import run_portfolio_freshness_job
from backend.jobs.state import JobStateStore
from backend.jobs.worker import ScheduledWorker, run_worker_once


class RecordingAlerts:
    def __init__(self):
        self.calls = []

    def alert(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "sent"}


class ScheduledWorkerTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.tmpdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmpdir.name, "trading_agent.db")
        db.ensure_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tmpdir.cleanup()

    @staticmethod
    def _monday_at_ist(hour, minute):
        return datetime(
            2026,
            8,
            3,
            hour,
            minute,
            tzinfo=ZoneInfo("Asia/Kolkata"),
        ).astimezone(timezone.utc)

    def test_fake_clock_schedule_is_ist_aware_and_weekday_bound(self):
        schedule = LocalSchedule(
            at=time(16, 0),
            weekdays=frozenset({0}),
            timezone_name="Asia/Kolkata",
        )

        before = datetime(2026, 8, 3, 10, 29, tzinfo=timezone.utc)
        due = datetime(2026, 8, 3, 10, 30, tzinfo=timezone.utc)
        sunday = datetime(2026, 8, 2, 10, 30, tzinfo=timezone.utc)

        self.assertIsNone(schedule.slot_for(before))
        slot = schedule.slot_for(due)
        self.assertIsNotNone(slot)
        self.assertEqual(slot.scheduled_at.isoformat(), "2026-08-03T16:00:00+05:30")
        self.assertEqual(slot.run_key, "2026-08-03T16:00+05:30")
        self.assertIsNone(schedule.slot_for(sunday))

    def test_due_slot_runs_once_with_idempotency(self):
        calls = []

        def fake_job(**kwargs):
            calls.append(kwargs)
            return {"status": "success", "source": "fake"}

        spec = JobSpec(
            name="fake_freshness",
            schedule=LocalSchedule(at=time(16, 0), timezone_name="Asia/Kolkata"),
            function=fake_job,
        )
        store = JobStateStore()
        worker = ScheduledWorker(
            specs=[spec],
            store=store,
            owner_id="worker-a",
            alert_sink=RecordingAlerts(),
            sleep=lambda _: None,
        )
        now = self._monday_at_ist(16, 1)

        first = worker.run_once(now=now)
        second = worker.run_once(now=now)

        self.assertEqual(len(calls), 1)
        self.assertEqual(first["jobs"][0]["status"], "success")
        self.assertEqual(second["jobs"][0]["status"], "idempotent")
        self.assertEqual(store.get_status("fake_freshness")["status"], "success")

    def test_lease_prevents_concurrent_workers(self):
        started = threading.Event()
        release = threading.Event()
        calls = []
        first_result = []

        def blocking_job(**kwargs):
            calls.append(kwargs)
            started.set()
            release.wait(timeout=5)
            return {"status": "success"}

        spec = JobSpec(
            name="concurrent_freshness",
            schedule=LocalSchedule(at=time(16, 0), timezone_name="Asia/Kolkata"),
            function=blocking_job,
        )
        worker_a = ScheduledWorker(
            specs=[spec],
            store=JobStateStore(),
            owner_id="worker-a",
            alert_sink=RecordingAlerts(),
        )
        worker_b = ScheduledWorker(
            specs=[spec],
            store=JobStateStore(),
            owner_id="worker-b",
            alert_sink=RecordingAlerts(),
        )
        now = self._monday_at_ist(16, 1)

        thread = threading.Thread(
            target=lambda: first_result.append(worker_a.run_once(now=now)),
            daemon=True,
        )
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        second = worker_b.run_once(now=now)
        release.set()
        thread.join(timeout=5)

        self.assertEqual(second["jobs"][0]["status"], "lease_held")
        self.assertEqual(len(calls), 1)
        self.assertEqual(first_result[0]["jobs"][0]["status"], "success")

    def test_retry_then_success_persists_attempt_count_and_is_idempotent(self):
        attempts = []

        def flaky_job(**kwargs):
            attempts.append(kwargs)
            if len(attempts) < 3:
                raise RuntimeError("temporary upstream failure")
            return {"status": "success", "attempts": len(attempts)}

        spec = JobSpec(
            name="retry_freshness",
            schedule=LocalSchedule(at=time(16, 0), timezone_name="Asia/Kolkata"),
            function=flaky_job,
            max_attempts=3,
            retry_delay_seconds=0,
        )
        store = JobStateStore()
        worker = ScheduledWorker(
            specs=[spec],
            store=store,
            owner_id="worker-a",
            alert_sink=RecordingAlerts(),
            sleep=lambda _: None,
        )
        now = self._monday_at_ist(16, 1)

        result = worker.run_once(now=now)
        repeat = worker.run_once(now=now)
        status = store.get_status("retry_freshness")

        self.assertEqual(result["jobs"][0]["status"], "success")
        self.assertEqual(result["jobs"][0]["attempt"], 3)
        self.assertEqual(repeat["jobs"][0]["status"], "idempotent")
        self.assertEqual(status["attempt"], 3)
        self.assertEqual(len(attempts), 3)

    def test_terminal_failure_is_retried_and_alerted_once(self):
        attempts = []
        alerts = RecordingAlerts()

        def failing_job(**kwargs):
            attempts.append(kwargs)
            raise RuntimeError("permanent upstream failure")

        spec = JobSpec(
            name="failed_freshness",
            schedule=LocalSchedule(at=time(16, 0), timezone_name="Asia/Kolkata"),
            function=failing_job,
            max_attempts=2,
            retry_delay_seconds=0,
        )
        worker = ScheduledWorker(
            specs=[spec],
            store=JobStateStore(),
            owner_id="worker-a",
            alert_sink=alerts,
            sleep=lambda _: None,
        )

        result = worker.run_once(now=self._monday_at_ist(16, 1))

        self.assertEqual(result["jobs"][0]["status"], "failed")
        self.assertEqual(result["jobs"][0]["attempt"], 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(alerts.calls), 1)
        self.assertEqual(alerts.calls[0]["status"], "failed")

    def test_missing_completion_emits_one_stale_freshness_alert(self):
        alerts = RecordingAlerts()
        spec = JobSpec(
            name="stale_freshness",
            schedule=LocalSchedule(at=time(16, 0), timezone_name="Asia/Kolkata"),
            function=lambda **kwargs: {"status": "success"},
            freshness_grace=timedelta(minutes=1),
        )
        worker = ScheduledWorker(
            specs=[spec],
            store=JobStateStore(),
            owner_id="worker-a",
            alert_sink=alerts,
        )
        now = self._monday_at_ist(16, 2)

        first = worker.check_freshness(now=now)
        second = worker.check_freshness(now=now)

        self.assertEqual(first[0]["status"], "stale")
        self.assertEqual(second[0]["alert"]["status"], "suppressed")
        self.assertEqual(len(alerts.calls), 1)
        self.assertEqual(alerts.calls[0]["status"], "stale")

    def test_missing_kite_token_skips_before_sync_review_or_telegram(self):
        notifier = Mock()
        review_builder = Mock()

        with (
            patch(
                "backend.brokers.kite.get_kite_status",
                return_value={"connected_today": False},
            ),
            patch("backend.positions.sync_positions_from_kite") as sync,
            patch("backend.positions.get_positions_view") as positions,
        ):
            result = run_portfolio_freshness_job(
                now=self._monday_at_ist(16, 1),
                provider=None,
                review_builder=review_builder,
                notifier=notifier,
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "kite_token_missing")
        sync.assert_not_called()
        positions.assert_not_called()
        review_builder.assert_not_called()
        notifier.assert_not_called()

    def test_one_shot_smoke_runs_all_jobs_with_mocked_services(self):
        calls = []
        service_fakes = {}
        for job_name in (
            "portfolio_freshness",
            "verdict_freshness",
            "outcome_freshness",
            "calendar_freshness",
        ):
            def fake(job_name=job_name, **kwargs):
                calls.append((job_name, kwargs["run_key"]))
                return {"status": "success", "mocked": True}

            service_fakes[job_name] = fake

        specs = build_job_specs(service_functions=service_fakes)
        result = run_worker_once(
            now=self._monday_at_ist(16, 31),
            specs=specs,
            store=JobStateStore(),
            alert_sink=RecordingAlerts(),
        )

        self.assertEqual(len(result["jobs"]), 4)
        self.assertTrue(all(job["status"] == "success" for job in result["jobs"]))
        self.assertEqual(len(calls), 4)

    def test_one_shot_smoke_exercises_services_with_external_calls_mocked(self):
        review = {"review_id": "mock-review"}
        with (
            patch.object(job_services, "CurrentKitePortfolioProvider") as provider,
            patch("backend.equity_portfolio.create_and_save_review", return_value=review),
            patch("backend.jobs.services.notify_portfolio_review", return_value={"status": "sent"}),
            patch(
                "backend.verdict_calibration.snapshot_today",
                return_value={"status": "ok", "date": "2026-08-03"},
            ),
            patch(
                "backend.verdict_calibration.backfill_outcomes",
                return_value={"status": "ok", "snapshots_updated": 1},
            ),
            patch(
                "backend.calendar_data.refresh_earnings_calendar",
                return_value={"status": "ok", "fetched": 100},
            ),
        ):
            provider.return_value.fetch.return_value = PortfolioSnapshot(
                status="ready",
                positions=[{"tradingsymbol": "RELIANCE"}],
                synced_at="2026-08-03T16:00:00+05:30",
            )
            result = run_worker_once(
                now=self._monday_at_ist(16, 31),
                specs=build_job_specs(),
                store=JobStateStore(),
                alert_sink=RecordingAlerts(),
            )

        self.assertEqual(len(result["jobs"]), 4)
        self.assertTrue(all(job["status"] == "success" for job in result["jobs"]))
        provider.return_value.fetch.assert_called_once()

    def test_worker_compose_declares_single_persistent_worker_service(self):
        compose = Path("deploy/docker-compose.prod.yml").read_text()

        self.assertIn("  worker:", compose)
        self.assertIn('command: ["python", "-m", "backend.jobs.worker"]', compose)
        self.assertIn("TRADING_AGENT_SCHEDULER_TIMEZONE", compose)
        self.assertIn("TRADING_AGENT_PROD_DATA_DIR", compose)
        self.assertIn("restart: unless-stopped", compose)
        self.assertNotIn("ports:", compose[compose.index("  worker:"):compose.index("  frontend:")])


if __name__ == "__main__":
    unittest.main()
