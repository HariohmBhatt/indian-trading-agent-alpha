#!/usr/bin/env python3
"""Validate deployment paths, database state, and backup configuration."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from urllib.parse import urlparse
from typing import Mapping

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.backup import validate_database
from backend.database_safety import (
    DatabaseNotFound,
    DatabaseSafetyError,
    InvalidDatabasePath,
    validate_data_path,
)


class DeploymentPreflightError(RuntimeError):
    """Raised when deployment cannot safely proceed."""


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Read KEY=VALUE lines without executing the env file."""

    env_path = Path(path).expanduser()
    if not env_path.is_absolute():
        raise DeploymentPreflightError(
            f"environment file must be absolute: {env_path!s}"
        )
    if not env_path.is_file():
        raise DeploymentPreflightError(
            f"environment file does not exist: {env_path!s}"
        )

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise DeploymentPreflightError(
                f"invalid environment assignment at {env_path}:{line_number}"
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            raise DeploymentPreflightError(
                f"invalid environment key at {env_path}:{line_number}"
            )
        try:
            tokens = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as exc:
            raise DeploymentPreflightError(
                f"invalid environment value at {env_path}:{line_number}"
            ) from exc
        values[key] = " ".join(tokens) if tokens else ""
    return values


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_backup_target(
    runtime_env: Mapping[str, str],
) -> dict[str, str | bool]:
    bucket = runtime_env.get("TRADINGAGENTS_BACKUP_S3_BUCKET", "").strip()
    if not bucket:
        raise DeploymentPreflightError(
            "TRADINGAGENTS_BACKUP_S3_BUCKET is required before deployment"
        )
    endpoint = runtime_env.get("TRADINGAGENTS_BACKUP_S3_ENDPOINT_URL", "").strip()
    safe_endpoint = ""
    if endpoint:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DeploymentPreflightError(
                "TRADINGAGENTS_BACKUP_S3_ENDPOINT_URL must be an http(s) URL"
            )
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        safe_endpoint = f"{parsed.scheme}://{host}"
    sse = runtime_env.get("TRADINGAGENTS_BACKUP_S3_SSE", "AES256").strip()
    if sse not in {"AES256", "aws:kms"}:
        raise DeploymentPreflightError(
            "TRADINGAGENTS_BACKUP_S3_SSE must be AES256 or aws:kms"
        )
    if sse == "aws:kms" and not runtime_env.get(
        "TRADINGAGENTS_BACKUP_S3_SSE_KMS_KEY_ID",
        "",
    ).strip():
        raise DeploymentPreflightError(
            "KMS backup encryption requires a key ID"
        )
    versioning_required = runtime_env.get(
        "TRADINGAGENTS_BACKUP_S3_REQUIRE_VERSIONING",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not versioning_required:
        raise DeploymentPreflightError(
            "database backups require S3 bucket versioning"
        )
    return {
        "bucket": bucket,
        "endpoint_url": safe_endpoint,
        "server_side_encryption": sse,
        "versioning_required": versioning_required,
    }


def preflight(
    *,
    data_dir: str | os.PathLike[str],
    db_path: str | os.PathLike[str] | None = None,
    runtime_env: Mapping[str, str] | None = None,
    require_existing_db: bool = True,
    require_backup_target: bool = True,
) -> dict[str, object]:
    """Run all local deployment checks and return a redacted report."""

    data_path = Path(data_dir).expanduser()
    if not data_path.is_absolute():
        raise InvalidDatabasePath(
            f"production data directory must be absolute: {data_path!s}"
        )
    if not data_path.exists() or not data_path.is_dir():
        raise DatabaseNotFound(
            f"production data directory does not exist: {data_path!s}"
        )
    data_real_path = data_path.resolve()
    if data_real_path == Path("/") or _is_under(
        data_real_path,
        ROOT_DIR.resolve(),
    ):
        raise InvalidDatabasePath(
            f"production data directory is unsafe: {data_path!s}"
        )

    environment = dict(runtime_env or {})
    configured_db = db_path or environment.get("TRADINGAGENTS_DB_PATH")
    database_path = Path(configured_db).expanduser() if configured_db else (
        data_path / "trading_agent.db"
    )
    database_path = validate_data_path(
        database_path,
        allow_missing=not require_existing_db,
    )
    if not _is_under(database_path.resolve(strict=False), data_real_path):
        raise InvalidDatabasePath(
            f"database path must be under the production data directory: "
            f"{database_path!s}"
        )

    database_state = "missing"
    if database_path.exists():
        validate_database(database_path)
        database_state = "present"
    elif require_existing_db:
        raise DatabaseNotFound(
            f"production database does not exist: {database_path!s}"
        )

    report: dict[str, object] = {
        "status": "ok",
        "data_dir": str(data_path),
        "db_path": str(database_path),
        "database": database_state,
    }
    if require_backup_target:
        report["backup_target"] = _validate_backup_target(environment)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deployment database and backup preflight checks."
    )
    parser.add_argument(
        "--data-dir",
        help="Absolute production data directory.",
    )
    parser.add_argument(
        "--db-path",
        help="Absolute database path; defaults to the data directory database.",
    )
    parser.add_argument(
        "--compose-env-file",
        type=Path,
        help="Host compose.env file used to discover production paths.",
    )
    parser.add_argument(
        "--runtime-env-file",
        type=Path,
        help="Runtime prod.env file containing backup target settings.",
    )
    parser.add_argument(
        "--allow-missing-db",
        action="store_true",
        help="Allow first-time database creation.",
    )
    parser.add_argument(
        "--skip-backup-target",
        action="store_true",
        help="Only validate local paths and database integrity.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        compose_env: dict[str, str] = {}
        if args.compose_env_file:
            compose_env = read_env_file(args.compose_env_file)
        data_dir = (
            args.data_dir
            or compose_env.get("TRADING_AGENT_PROD_DATA_DIR")
            or os.getenv("TRADING_AGENT_PROD_DATA_DIR")
        )
        if not data_dir:
            raise DeploymentPreflightError(
                "production data directory is required"
            )

        runtime_env: dict[str, str] = {}
        runtime_path = (
            args.runtime_env_file
            or (
                Path(compose_env["TRADING_AGENT_PROD_ENV_FILE"])
                if compose_env.get("TRADING_AGENT_PROD_ENV_FILE")
                else None
            )
        )
        if runtime_path:
            runtime_env.update(read_env_file(runtime_path))
        runtime_env.update(
            {
                key: value
                for key, value in os.environ.items()
                if key.startswith("TRADINGAGENTS_BACKUP_S3_")
                or key == "TRADINGAGENTS_DB_PATH"
            }
        )

        report = preflight(
            data_dir=data_dir,
            db_path=args.db_path,
            runtime_env=runtime_env,
            require_existing_db=not args.allow_missing_db,
            require_backup_target=not args.skip_backup_target,
        )
        print(json.dumps(report, sort_keys=True))
        return 0
    except (DatabaseSafetyError, DeploymentPreflightError, ValueError) as exc:
        print(f"deployment preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
