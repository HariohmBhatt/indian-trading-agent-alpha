import asyncio
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = "/home/hariohm/.config/indian-trading-agent"


class ComposeSecurityTests(unittest.TestCase):
    def _compose_config(self, compose_file, environment):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / "compose-test.env"
            env_file.write_text(
                "\n".join(f"{key}={value}" for key, value in environment.items()) + "\n"
            )
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    str(env_file),
                    "-f",
                    str(ROOT / compose_file),
                    "config",
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(result.stdout)

    def test_development_ports_bind_only_to_loopback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dev_env = Path(tmpdir) / "dev.env"
            dev_env.touch()
            config = self._compose_config(
                "deploy/docker-compose.dev.yml",
                {"TRADING_AGENT_DEV_ENV_FILE": dev_env},
            )

        for service, expected_port in (("backend", 8000), ("frontend", 3000)):
            port = config["services"][service]["ports"][0]
            self.assertEqual(port["host_ip"], "127.0.0.1")
            self.assertEqual(port["target"], expected_port)

    def test_container_listeners_remain_wildcard_bound(self):
        backend_dockerfile = (ROOT / "deploy/docker/backend.Dockerfile").read_text()
        frontend_dockerfile = (ROOT / "deploy/docker/frontend.Dockerfile").read_text()

        self.assertIn('"--host", "0.0.0.0"', backend_dockerfile)
        self.assertIn('"--hostname", "0.0.0.0"', frontend_dockerfile)

    def test_runner_exposes_only_required_configuration_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner_env = Path(tmpdir) / "runner.env"
            runner_env.write_text("RUNNER_REPO_URL=https://github.com/example/repo\n")
            config = self._compose_config(
                "deploy/docker-compose.runner.yml",
                {
                    "TRADING_AGENT_RUNNER_ENV_FILE": runner_env,
                    "RUNNER_REPO_URL": "https://github.com/example/repo",
                },
            )

        volumes = config["services"]["runner"]["volumes"]
        binds = {volume["source"]: volume["target"] for volume in volumes if volume["type"] == "bind"}
        self.assertEqual(
            set(binds),
            {
                "/var/run/docker.sock",
                f"{CONFIG_DIR}/compose.env",
                f"{CONFIG_DIR}/prod.env",
            },
        )
        self.assertNotIn(CONFIG_DIR, binds)
        self.assertNotIn(f"{CONFIG_DIR}/cloudflared-config.yml", binds)
        self.assertNotIn(f"{CONFIG_DIR}/cloudflared-credentials.json", binds)

    def test_frontend_dev_origins_are_loopback_only(self):
        source = (ROOT / "frontend/next.config.ts").read_text()

        self.assertIn('"localhost"', source)
        self.assertIn('"127.0.0.1"', source)
        for remote_origin in ("dellg15", "192.168.29.225", "192.168.29.213", "100.91.136.0"):
            self.assertNotIn(remote_origin, source)


class CorsSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from backend.app import app

        cls.app = app

    def _request(self, method, origin, requested_method=None):
        headers = [(b"origin", origin.encode())]
        if requested_method:
            headers.append((b"access-control-request-method", requested_method.encode()))
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/api/health",
            "raw_path": b"/api/health",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        }
        asyncio.run(self.app(scope, receive, send))
        start = next(message for message in messages if message["type"] == "http.response.start")
        return start["status"], dict(start["headers"])

    def test_loopback_origins_are_allowed(self):
        for origin in (
            "http://localhost:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:3001",
        ):
            with self.subTest(origin=origin):
                status, headers = self._request("OPTIONS", origin, "GET")
                self.assertEqual(status, 200)
                self.assertEqual(headers[b"access-control-allow-origin"], origin.encode())

    def test_non_loopback_origins_are_denied(self):
        for origin in (
            "http://dellg15:3000",
            "http://192.168.29.225:3000",
            "http://100.91.136.0:3000",
        ):
            with self.subTest(origin=origin):
                status, headers = self._request("OPTIONS", origin, "GET")
                self.assertEqual(status, 400)
                self.assertNotIn(b"access-control-allow-origin", headers)


class RunnerEntrypointTests(unittest.TestCase):
    entrypoint = ROOT / "deploy/runner/entrypoint.sh"

    @staticmethod
    def _write_executable(path, contents):
        path.write_text(contents)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run_fixture(self, registered, token=None):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runner_dir = root / "runner"
            runner_home = root / "home"
            fake_bin = root / "bin"
            runner_dir.mkdir()
            fake_bin.mkdir()
            if registered:
                (runner_dir / ".runner").touch()

            config_token = root / "config-token"
            runtime_token = root / "runtime-token"
            config_marker = root / "config-ran"
            self._write_executable(
                runner_dir / "config.sh",
                f"""#!/usr/bin/env bash
printf '%s' "${{RUNNER_TOKEN-}}" > {config_token}
touch {config_marker}
touch .runner
""",
            )
            self._write_executable(
                runner_dir / "run.sh",
                f"""#!/usr/bin/env bash
printf '%s' "${{RUNNER_TOKEN-unset}}" > {runtime_token}
""",
            )
            self._write_executable(
                fake_bin / "chown",
                "#!/bin/sh\nexit 0\n",
            )
            self._write_executable(
                fake_bin / "getent",
                "#!/bin/sh\nprintf 'host-docker:x:99999:\\n'\n",
            )
            self._write_executable(
                fake_bin / "groupadd",
                "#!/bin/sh\nexit 0\n",
            )
            self._write_executable(
                fake_bin / "stat",
                "#!/bin/sh\nprintf '99999\\n'\n",
            )
            self._write_executable(
                fake_bin / "usermod",
                "#!/bin/sh\nexit 0\n",
            )
            self._write_executable(
                fake_bin / "runuser",
                """#!/bin/sh
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
""",
            )

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "RUNNER_REPO_URL": "https://github.com/example/repo",
                    "RUNNER_DIR": str(runner_dir),
                    "RUNNER_HOME": str(runner_home),
                }
            )
            if token is None:
                environment.pop("RUNNER_TOKEN", None)
            else:
                environment["RUNNER_TOKEN"] = token

            result = subprocess.run(
                [str(self.entrypoint)],
                env=environment,
                capture_output=True,
                text=True,
            )
            runtime_value = runtime_token.read_text() if runtime_token.exists() else None
            config_value = config_token.read_text() if config_token.exists() else None
            return result, config_marker.exists(), config_value, runtime_value

    def test_first_start_requires_token_and_does_not_keep_it(self):
        result, config_ran, config_token, runtime_token = self._run_fixture(
            registered=False,
            token="registration-secret",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(config_ran)
        self.assertEqual(config_token, "registration-secret")
        self.assertEqual(runtime_token, "unset")

        result, config_ran, _, _ = self._run_fixture(registered=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(config_ran)
        self.assertIn("RUNNER_TOKEN is required on the first start", result.stderr)

    def test_existing_registration_reconnects_without_token(self):
        result, config_ran, config_token, runtime_token = self._run_fixture(registered=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(config_ran)
        self.assertIsNone(config_token)
        self.assertEqual(runtime_token, "unset")


class WorkflowTrustBoundaryTests(unittest.TestCase):
    def test_self_hosted_workflows_have_no_pull_request_trigger(self):
        workflow_dir = ROOT / ".github/workflows"
        self_hosted_workflows = [
            path
            for path in workflow_dir.iterdir()
            if path.suffix in {".yml", ".yaml"}
            and "self-hosted" in path.read_text()
        ]
        self.assertTrue(self_hosted_workflows)
        for workflow in self_hosted_workflows:
            contents = workflow.read_text()
            self.assertNotRegex(
                contents,
                re.compile(r"(?m)^\s*pull_request(?:_target)?\s*:"),
            )
            self.assertNotRegex(
                contents,
                re.compile(r"(?m)^\s*-\s*pull_request(?:_target)?\s*$"),
            )
            self.assertNotIn("[pull_request", contents)


class ProductionEnvironmentTests(unittest.TestCase):
    def test_repository_dotenv_cannot_override_injected_values(self):
        source = (ROOT / "backend/app.py").read_text()

        self.assertIn("load_dotenv(os.path.join(PROJECT_ROOT, \".env\"), override=False)", source)
        self.assertNotIn("override=True", source)
