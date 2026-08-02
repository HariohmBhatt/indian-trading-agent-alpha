from __future__ import annotations

import datetime as dt
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/recovery_acceptance.py"
FIXTURE_CREATOR = ROOT / "scripts/create_disposable_backup_fixture.py"
WRAPPER = ROOT / "scripts/phase10-recovery-acceptance.sh"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


class PublicFixtureHandler(QuietHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/api/health":
            body = json.dumps(
                {"status": "ok", "service": "disposable-fixture"}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/":
            body = b"<html><body>disposable fixture</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class RecoveryAcceptanceTests(unittest.TestCase):
    def run_checker(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CHECKER), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def create_fixture(
        self,
        directory: Path,
        *,
        created_at: dt.datetime | None = None,
    ) -> Path:
        output = directory / "fixtures/backup.tar.gz"
        command = [
            sys.executable,
            str(FIXTURE_CREATOR),
            "--output",
            str(output),
        ]
        if created_at is not None:
            command.extend(["--created-at", created_at.isoformat()])
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.is_file())
        return output

    def start_server(
        self,
        directory: Path,
        *,
        public: bool = False,
    ) -> tuple[ThreadingHTTPServer, threading.Thread]:
        handler = PublicFixtureHandler if public else partial(
            QuietHandler, directory=str(directory)
        )
        if public:
            handler = partial(PublicFixtureHandler, directory=str(directory))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_disposable_s3_restore_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.create_fixture(directory)
            server, thread = self.start_server(directory)
            try:
                result = self.run_checker(
                    "restore",
                    "--s3-endpoint",
                    f"http://127.0.0.1:{server.server_port}",
                    "--bucket",
                    "fixtures",
                    "--key",
                    "backup.tar.gz",
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("[PASS     ] backup.freshness", result.stdout)
            self.assertIn("[PASS     ] backup.integrity", result.stdout)

    def test_stale_backup_fails_without_copying(self) -> None:
        stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = self.create_fixture(directory, created_at=stale)
            target = directory / "indian-trading-agent-disposable-restore"
            result = self.run_checker(
                "restore",
                "--archive",
                str(archive),
                "--apply",
                "--target-dir",
                str(target),
                "--i-understand-disposable",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("[FAIL     ] backup.freshness", result.stdout)
            self.assertFalse(target.exists())

    def test_production_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = self.create_fixture(Path(temporary))
            target = Path.home() / ".tradingagents-prod"
            result = self.run_checker(
                "restore",
                "--archive",
                str(archive),
                "--apply",
                "--target-dir",
                str(target),
                "--i-understand-disposable",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("known production data directory", result.stdout)

    def test_archive_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                member = tarfile.TarInfo("../escape.txt")
                member.size = len(b"not safe")
                from io import BytesIO

                bundle.addfile(member, BytesIO(b"not safe"))
            result = self.run_checker("restore", "--archive", str(archive))
            self.assertEqual(result.returncode, 1)
            self.assertIn("unsafe archive member path", result.stdout)
            self.assertFalse((directory.parent / "escape.txt").exists())

    def test_public_fixture_validation_and_missing_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, thread = self.start_server(Path(temporary), public=True)
            try:
                result = self.run_checker(
                    "public",
                    "--url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--allow-http",
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("[PASS     ] public.frontend", result.stdout)
            self.assertIn("[PASS     ] public.api", result.stdout)

        missing = self.run_checker("public")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("[DEFERRED ] public.endpoint", missing.stdout)

    def test_provenance_policy_requires_digest_and_attestation(self) -> None:
        backend_digest = "a" * 64
        frontend_digest = "b" * 64
        with tempfile.TemporaryDirectory() as temporary:
            attestation = Path(temporary) / "provenance.json"
            attestation.write_text(
                json.dumps({"digests": [backend_digest, frontend_digest]}),
                encoding="utf-8",
            )
            valid = self.run_checker(
                "provenance",
                "--image",
                f"ghcr.io/example/backend@sha256:{backend_digest}",
                "--image",
                f"ghcr.io/example/frontend@sha256:{frontend_digest}",
                "--attestation-file",
                str(attestation),
                "--strict",
            )
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
            self.assertIn("[PASS     ] image.provenance", valid.stdout)

        mutable = self.run_checker(
            "provenance",
            "--image",
            "ghcr.io/example/backend:latest",
        )
        self.assertEqual(mutable.returncode, 1)
        self.assertIn("mutable GHCR", mutable.stdout)

        base_branch = self.run_checker(
            "provenance",
            "--compose-file",
            "deploy/docker-compose.prod.yml",
        )
        self.assertEqual(base_branch.returncode, 2)
        self.assertIn("local/tagged", base_branch.stdout)

    def test_runner_missing_prerequisite_and_rollback_plan(self) -> None:
        runner = self.run_checker("runner")
        self.assertEqual(runner.returncode, 2)
        self.assertIn("runner env file", runner.stdout)

        current = "c" * 64
        previous = "d" * 64
        rollback = self.run_checker(
            "rollback",
            "--current-image",
            f"ghcr.io/example/backend@sha256:{current}",
            "--previous-image",
            f"ghcr.io/example/backend@sha256:{previous}",
            "--compose-file",
            "deploy/docker-compose.prod.yml",
        )
        self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
        self.assertIn("[PASS     ] rollback.dry_run", rollback.stdout)

    def test_read_only_wrapper_rejects_restore_mutation(self) -> None:
        result = subprocess.run(
            [str(WRAPPER), "--apply"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("read-only", result.stderr)
