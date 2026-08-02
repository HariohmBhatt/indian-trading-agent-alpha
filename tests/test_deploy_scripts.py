import http.server
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / "deploy" / "validate-prod.sh"
ROLLBACK = ROOT / "deploy" / "rollback.sh"


class SmokeHandler(http.server.BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        self.__class__.requests.append((self.path, self.headers.get("CF-Access-Client-Id"), self.headers.get("CF-Access-Client-Secret")))
        if self.path == "/metrics":
            body = b"# HELP cloudflared_tunnel_ha_connections Active connections\ncloudflared_tunnel_ha_connections 1\n"
        elif self.path == "/ready":
            body = b"OK\n"
        else:
            body = b'{"status":"ok"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class DeploymentScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.fake_docker = self.bin_dir / "docker"
        self.fake_docker.write_text(
            """#!/usr/bin/env bash
set -eu
if [[ "$1" == "compose" ]]; then
  shift
  while (($#)); do
    case "$1" in
      --env-file|-f) shift 2 ;;
      *) break ;;
    esac
  done
  command="$1"
  shift
  case "$command" in
    config) exit 0 ;;
    ps)
      service="${@: -1}"
      printf 'container-%s\\n' "$service"
      exit 0
      ;;
    exec) exit 0 ;;
    *) exit 0 ;;
  esac
fi
if [[ "$1" == "inspect" ]]; then
  if [[ "$2" == "--format" ]]; then
    format="$3"
    case "$format" in
      *".State.Status"*) printf 'running\\n' ;;
      *".State.Health"*) printf 'healthy\\n' ;;
      *".Config.Env"*) printf 'TRADING_AGENT_RELEASE_SHA=abc1234\\n' ;;
      *".Image"*) printf 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n' ;;
      *".Config.Labels"*) printf 'abc1234\\n' ;;
      *) printf '\\n' ;;
    esac
  fi
  exit 0
fi
exit 0
"""
        )
        self.fake_docker.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        self.compose_env = self.root / "compose.env"
        self.compose_env.write_text("CLOUDFLARED_METRICS_PORT=20241\n")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_mocked_tunnel_and_access_aware_public_smoke(self):
        SmokeHandler.requests = []
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SmokeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client_id = self.root / "access-client-id"
            client_secret = self.root / "access-client-secret"
            client_id.write_text("client-id-not-for-output")
            client_secret.write_text("client-secret-not-for-output")
            client_id.chmod(0o600)
            client_secret.chmod(0o600)
            self.compose_env.write_text(
                f"CLOUDFLARED_METRICS_PORT={server.server_port}\n"
                f"TRADING_AGENT_PUBLIC_BASE_URL=http://127.0.0.1:{server.server_port}\n"
                f"TRADING_AGENT_ACCESS_CLIENT_ID_FILE={client_id}\n"
                f"TRADING_AGENT_ACCESS_CLIENT_SECRET_FILE={client_secret}\n"
            )
            env = {
                **os.environ,
                "DOCKER_BIN": str(self.fake_docker),
            }
            result = subprocess.run(
                [
                    str(VALIDATE),
                    "--public",
                    "--expected-sha",
                    "abc1234",
                    "--compose-env-file",
                    str(self.compose_env),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("client-id-not-for-output", result.stdout + result.stderr)
        self.assertNotIn("client-secret-not-for-output", result.stdout + result.stderr)
        public_paths = {request[0] for request in SmokeHandler.requests if request[1]}
        self.assertEqual(public_paths, {"/health", "/api/health", "/api/ready"})
        self.assertTrue(all(request[2] == "client-secret-not-for-output" for request in SmokeHandler.requests if request[1]))

    def test_rollback_to_previous_digest_dry_run_is_non_mutating(self):
        release_dir = self.root / "releases"
        release_dir.mkdir()
        digest = "sha256:" + "b" * 64
        manifest = {
            "schema_version": 1,
            "release_sha": "abc1234",
            "deployed_at": "2026-08-02T00:00:00Z",
            "services": {
                "backend": {"image_ref": "backend:prod", "image_digest": digest},
                "frontend": {"image_ref": "frontend:prod", "image_digest": digest},
                "cloudflared": {"image_ref": "cloudflare/cloudflared:latest", "image_digest": digest},
            },
        }
        manifest_path = release_dir / "last-known-good.json"
        manifest_path.write_text(json.dumps(manifest))
        self.compose_env.write_text(f"TRADING_AGENT_RELEASE_DIR={release_dir}\n")

        env = {**os.environ, "DOCKER_BIN": str(self.fake_docker)}
        result = subprocess.run(
            [str(ROLLBACK), "--dry-run", "--compose-env-file", str(self.compose_env)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(digest, result.stdout)
        self.assertIn("no containers, images, or database state changed", result.stdout)
        self.assertFalse((release_dir / "current.json").exists())


if __name__ == "__main__":
    unittest.main()
