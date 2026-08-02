#!/usr/bin/env python3
"""Read-only recovery and release acceptance checks for disposable fixtures.

This module deliberately does not deploy, promote, roll back, or contact a
production host.  Checks that need an operator-controlled prerequisite return
``DEFERRED`` rather than guessing that an unmerged phase is available.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen
import zipfile


PASS = "PASS"
FAIL = "FAIL"
DEFERRED = "DEFERRED"
EXIT_FAILURE = 1
EXIT_DEFERRED = 2

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
IMAGE_DIGEST_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$"
)
GHCR_DIGEST_RE = re.compile(
    r"^ghcr\.io/[a-z0-9]+(?:[._/-][a-z0-9._/-]*)*@sha256:[0-9a-f]{64}$"
)
COMPOSE_VARIABLE_RE = re.compile(
    r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}$"
)


class AcceptanceFailure(Exception):
    """A supplied fixture or release candidate failed an acceptance check."""


class DeferredPrerequisite(Exception):
    """A check cannot run until an operator supplies a prerequisite."""


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class ArchiveInspection:
    manifest: dict[str, Any]
    created_at: dt.datetime
    age_hours: float
    database_path: Path
    file_count: int


def check(
    name: str,
    status: str,
    detail: str,
    evidence: Iterable[str] = (),
) -> Check:
    return Check(name, status, detail, tuple(evidence))


def parse_timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceFailure("manifest created_at must be an ISO-8601 string")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AcceptanceFailure(f"invalid manifest created_at: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(name: str) -> Path:
    """Return a safe relative path or reject archive traversal/special names."""

    if not name or "\\" in name:
        raise AcceptanceFailure(f"unsafe archive member path: {name!r}")
    posix_path = PurePosixPath(name)
    if posix_path.is_absolute() or ".." in posix_path.parts:
        raise AcceptanceFailure(f"unsafe archive member path: {name!r}")
    if str(posix_path) in {"", "."}:
        raise AcceptanceFailure(f"empty archive member path: {name!r}")
    return Path(*posix_path.parts)


def _write_archive_member(destination: Path, relative: Path, stream: Any) -> None:
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise AcceptanceFailure(f"duplicate archive member: {relative}")
    with target.open("wb") as output:
        shutil.copyfileobj(stream, output)


def extract_archive(archive: Path, destination: Path) -> None:
    """Extract a tar/zip backup without following links or traversal paths."""

    destination.mkdir(parents=True, exist_ok=True)
    member_names: set[str] = set()

    if tarfile.is_tarfile(archive):
        with tarfile.open(archive, mode="r:*") as bundle:
            for member in bundle.getmembers():
                relative = _safe_relative_path(member.name)
                normalized = relative.as_posix()
                if normalized in member_names:
                    raise AcceptanceFailure(f"duplicate archive member: {member.name}")
                member_names.add(normalized)
                if member.isdir():
                    (destination / relative).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise AcceptanceFailure(
                        f"archive member is not a regular file: {member.name}"
                    )
                source = bundle.extractfile(member)
                if source is None:
                    raise AcceptanceFailure(f"could not read archive member: {member.name}")
                with source:
                    _write_archive_member(destination, relative, source)
        return

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                relative = _safe_relative_path(member.filename)
                normalized = relative.as_posix()
                if normalized in member_names:
                    raise AcceptanceFailure(f"duplicate archive member: {member.filename}")
                member_names.add(normalized)
                mode = (member.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise AcceptanceFailure(
                        f"archive member is a symbolic link: {member.filename}"
                    )
                if member.is_dir():
                    (destination / relative).mkdir(parents=True, exist_ok=True)
                    continue
                with bundle.open(member, "r") as source:
                    _write_archive_member(destination, relative, source)
        return

    raise AcceptanceFailure(
        f"unsupported backup archive {archive.name}; expected .tar/.tar.gz/.zip"
    )


def _load_manifest(restored_root: Path) -> dict[str, Any]:
    manifest_path = restored_root / "manifest.json"
    if not manifest_path.is_file():
        raise AcceptanceFailure("backup archive is missing root manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceFailure(f"could not parse manifest.json: {exc}") from exc
    if not isinstance(manifest, dict):
        raise AcceptanceFailure("manifest.json must contain an object")
    if str(manifest.get("schema_version")) != "1":
        raise AcceptanceFailure("manifest schema_version must be 1")
    return manifest


def _manifest_files(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise AcceptanceFailure("manifest files must be a non-empty list")
    entries: list[dict[str, Any]] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise AcceptanceFailure("each manifest file entry must be an object")
        if not isinstance(entry.get("path"), str):
            raise AcceptanceFailure("each manifest file entry needs a path")
        if not isinstance(entry.get("sha256"), str) or not SHA256_RE.fullmatch(
            entry["sha256"]
        ):
            raise AcceptanceFailure(
                f"manifest sha256 is invalid for {entry.get('path')!r}"
            )
        _safe_relative_path(entry["path"])
        entries.append(entry)
    return entries


def _verify_sqlite(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise AcceptanceFailure(f"SQLite integrity check failed for {path}: {exc}") from exc
    if not result or result[0] != "ok":
        raise AcceptanceFailure(f"SQLite integrity check failed for {path}: {result!r}")


def inspect_restored_archive(
    restored_root: Path,
    *,
    max_age_hours: float,
) -> tuple[list[Check], ArchiveInspection | None]:
    """Validate freshness, checksums, and the restored SQLite database."""

    try:
        manifest = _load_manifest(restored_root)
    except AcceptanceFailure as exc:
        return [check("backup.integrity", FAIL, str(exc))], None

    try:
        created_at = parse_timestamp(
            manifest.get("created_at") or manifest.get("backup_at")
        )
        age_hours = (dt.datetime.now(dt.timezone.utc) - created_at).total_seconds() / 3600
        if age_hours < -0.0833:
            freshness = check(
                "backup.freshness",
                FAIL,
                "backup timestamp is more than five minutes in the future",
            )
        elif age_hours > max_age_hours:
            freshness = check(
                "backup.freshness",
                FAIL,
                f"backup is {age_hours:.2f} hours old; limit is {max_age_hours:.2f} hours",
            )
        else:
            freshness = check(
                "backup.freshness",
                PASS,
                f"backup is {max(age_hours, 0):.2f} hours old within {max_age_hours:.2f}-hour limit",
                (f"created_at={created_at.isoformat()}",),
            )
    except AcceptanceFailure as exc:
        freshness = check("backup.freshness", FAIL, str(exc))
        created_at = dt.datetime.now(dt.timezone.utc)
        age_hours = float("inf")

    integrity_errors: list[str] = []
    database_path: Path | None = None
    file_entries: list[dict[str, Any]] = []
    try:
        file_entries = _manifest_files(manifest)
        for entry in file_entries:
            relative = _safe_relative_path(entry["path"])
            path = restored_root / relative
            if not path.is_file():
                raise AcceptanceFailure(f"manifest file is missing: {entry['path']}")
            if "size" in entry and entry["size"] != path.stat().st_size:
                raise AcceptanceFailure(f"size mismatch for {entry['path']}")
            actual_sha256 = sha256_file(path)
            if actual_sha256.lower() != entry["sha256"].lower():
                raise AcceptanceFailure(f"checksum mismatch for {entry['path']}")

        declared_database = manifest.get("database")
        if declared_database is None:
            raise AcceptanceFailure("manifest database must name a SQLite file")
        if not isinstance(declared_database, str):
            raise AcceptanceFailure("manifest database must be a relative path")
        database_path = restored_root / _safe_relative_path(declared_database)
        if not database_path.is_file():
            raise AcceptanceFailure(f"declared database is missing: {declared_database}")
        _verify_sqlite(database_path)
    except (AcceptanceFailure, OSError) as exc:
        integrity_errors.append(str(exc))

    if integrity_errors:
        integrity = check("backup.integrity", FAIL, "; ".join(integrity_errors))
        inspection = None
    else:
        integrity = check(
            "backup.integrity",
            PASS,
            f"verified {len(file_entries)} file checksum(s) and SQLite integrity",
            (f"database={database_path.relative_to(restored_root)}",),
        )
        inspection = ArchiveInspection(
            manifest=manifest,
            created_at=created_at,
            age_hours=age_hours,
            database_path=database_path,
            file_count=len(file_entries),
        )

    return [freshness, integrity], inspection


def _s3_object_url(endpoint: str, bucket: str, key: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AcceptanceFailure("S3 endpoint must be an absolute http(s) URL")
    base = endpoint.rstrip("/") + "/"
    return urljoin(base, f"{quote(bucket, safe='')}/{quote(key.lstrip('/'), safe='/')}")


def download_backup(
    *,
    archive: str | None,
    s3_endpoint: str | None,
    bucket: str | None,
    key: str | None,
    download_directory: Path,
) -> Path:
    if archive and s3_endpoint:
        raise AcceptanceFailure("choose --archive or --s3-endpoint, not both")
    if archive:
        archive_path = Path(archive).expanduser()
        if not archive_path.is_file():
            raise AcceptanceFailure(f"backup archive does not exist: {archive_path}")
        return archive_path
    if not s3_endpoint:
        raise DeferredPrerequisite(
            "supply --archive or --s3-endpoint/--bucket/--key for a disposable backup"
        )
    if not bucket or not key:
        raise DeferredPrerequisite(
            "--s3-endpoint requires both --bucket and --key"
        )

    url = _s3_object_url(s3_endpoint, bucket, key)
    target = download_directory / "downloaded-backup"
    request = Request(url, headers={"User-Agent": "phase10-recovery-acceptance/1"})
    try:
        with urlopen(request, timeout=20) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output)
    except HTTPError as exc:
        raise AcceptanceFailure(f"S3-compatible endpoint returned HTTP {exc.code}") from exc
    except (OSError, URLError) as exc:
        raise AcceptanceFailure(f"could not download backup from S3 endpoint: {exc}") from exc
    return target


def _copy_verified_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        else:
            raise AcceptanceFailure(f"unexpected extracted fixture entry: {relative}")


def _validate_disposable_target(target: Path, allow_existing: bool) -> None:
    if not target.is_absolute():
        raise AcceptanceFailure("--target-dir must be an absolute disposable path")
    resolved = target.resolve(strict=False)
    protected = {
        (Path.home() / ".tradingagents").resolve(strict=False),
        (Path.home() / ".tradingagents-prod").resolve(strict=False),
    }
    configured = os.environ.get("TRADING_AGENT_PROD_DATA_DIR")
    if configured:
        protected.add(Path(configured).expanduser().resolve(strict=False))
    if resolved in protected or any(path in resolved.parents for path in protected):
        raise AcceptanceFailure("refusing to use a known production data directory")
    label = resolved.name.lower()
    if "disposable" not in label and "restore" not in label:
        raise AcceptanceFailure(
            "target directory name must contain 'disposable' or 'restore'"
        )
    if resolved.exists() and any(resolved.iterdir()) and not allow_existing:
        raise AcceptanceFailure(
            "target directory is non-empty; use a new disposable path or "
            "--allow-existing-disposable"
        )


def run_archive_check(args: argparse.Namespace, *, restore: bool) -> list[Check]:
    try:
        with tempfile.TemporaryDirectory(prefix="phase10-download-") as download_dir:
            archive = download_backup(
                archive=args.archive,
                s3_endpoint=args.s3_endpoint,
                bucket=args.bucket,
                key=args.key,
                download_directory=Path(download_dir),
            )
            with tempfile.TemporaryDirectory(prefix="phase10-restore-") as restored_dir:
                restored_root = Path(restored_dir)
                try:
                    extract_archive(archive, restored_root)
                except AcceptanceFailure as exc:
                    return [check("restore.integrity", FAIL, str(exc))]

                results, inspection = inspect_restored_archive(
                    restored_root,
                    max_age_hours=args.max_age_hours,
                )
                if (
                    restore
                    and args.apply
                    and inspection is not None
                    and all(item.status == PASS for item in results)
                ):
                    if not args.i_understand_disposable:
                        return results + [
                            check(
                                "restore.target",
                                FAIL,
                                "applying a restore requires --i-understand-disposable",
                            )
                        ]
                    target = Path(args.target_dir).expanduser()
                    try:
                        _validate_disposable_target(
                            target, args.allow_existing_disposable
                        )
                        _copy_verified_tree(restored_root, target)
                    except AcceptanceFailure as exc:
                        results.append(check("restore.target", FAIL, str(exc)))
                    else:
                        results.append(
                            check(
                                "restore.target",
                                PASS,
                                "verified archive copied to disposable target",
                                (f"target={target}",),
                            )
                        )
                elif restore and args.apply:
                    results.append(
                        check(
                            "restore.target",
                            DEFERRED,
                            "target was not modified because archive checks did not pass",
                        )
                    )
                elif restore and args.target_dir:
                    results.append(
                        check(
                            "restore.target",
                            FAIL,
                            "--target-dir is only valid with explicit --apply",
                        )
                    )
                return results
    except DeferredPrerequisite as exc:
        return [check("backup.source", DEFERRED, str(exc))]
    except AcceptanceFailure as exc:
        return [check("backup.source", FAIL, str(exc))]


def _resolve_compose_value(value: str) -> str:
    match = COMPOSE_VARIABLE_RE.fullmatch(value)
    if not match:
        return value.strip("'\"")
    name = match.group("name")
    default = match.group("default")
    return os.environ.get(name, default if default is not None else value)


def compose_image_refs(compose_file: Path) -> list[str]:
    refs: list[str] = []
    for line in compose_file.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*image:\s*(\S+)\s*$", line)
        if match:
            refs.append(_resolve_compose_value(match.group(1)))
    return refs


def provenance_results(args: argparse.Namespace) -> list[Check]:
    source = "explicit image arguments"
    strict = getattr(args, "strict", getattr(args, "strict_provenance", False))
    try:
        if args.images:
            refs = list(args.images)
        elif args.compose_file:
            compose_file = Path(args.compose_file)
            if not compose_file.is_file():
                return [check("image.provenance", FAIL, f"missing compose file: {compose_file}")]
            source = str(compose_file)
            refs = compose_image_refs(compose_file)
        else:
            return [
                check(
                    "image.provenance",
                    DEFERRED,
                    "supply --image or --compose-file for a release candidate",
                )
            ]
    except OSError as exc:
        return [check("image.provenance", FAIL, f"could not read image source: {exc}")]

    if not refs:
        return [check("image.provenance", DEFERRED, f"no image refs found in {source}")]

    invalid = [ref for ref in refs if not IMAGE_DIGEST_RE.fullmatch(ref)]
    if invalid:
        if args.images or all(ref.startswith("ghcr.io/") for ref in invalid):
            return [
                check(
                    "image.provenance",
                    FAIL,
                    "mutable GHCR or registry image reference(s) are not promotable",
                    tuple(f"invalid={ref.split('@', 1)[0]}" for ref in invalid),
                )
            ]
        return [
            check(
                "image.provenance",
                DEFERRED,
                "candidate still uses local/tagged images; immutable digest promotion is a prerequisite",
                tuple(f"image={ref}" for ref in refs),
            )
        ]

    digests = [ref.rsplit(":", 1)[-1] for ref in refs]
    if args.attestation_file:
        attestation = Path(args.attestation_file).expanduser()
        if not attestation.is_file():
            return [
                check(
                    "image.provenance",
                    FAIL if strict else DEFERRED,
                    f"attestation file is missing: {attestation}",
                )
            ]
        try:
            attestation_text = attestation.read_text(encoding="utf-8")
        except OSError as exc:
            return [check("image.provenance", FAIL, f"could not read attestation: {exc}")]
        missing = [digest for digest in digests if digest not in attestation_text]
        if missing:
            return [
                check(
                    "image.provenance",
                    FAIL,
                    "attestation does not cover every promoted image digest",
                    tuple(f"missing_digest={digest}" for digest in missing),
                )
            ]
        return [
            check(
                "image.provenance",
                PASS,
                f"{len(refs)} immutable image(s) are digest-pinned and covered by the supplied attestation",
            )
        ]

    return [
        check(
            "image.provenance",
            FAIL if strict else DEFERRED,
            "digest-pinned refs are present, but provenance attestation evidence was not supplied",
            ("run cosign verification and pass its recorded JSON evidence",),
        )
    ]


def _http_get(url: str, timeout: float) -> tuple[int, str, bytes]:
    request = Request(url, headers={"User-Agent": "phase10-recovery-acceptance/1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.geturl(), response.read(1024 * 1024)
    except HTTPError as exc:
        return exc.code, exc.geturl(), exc.read(1024 * 1024)
    except (OSError, URLError) as exc:
        raise AcceptanceFailure(f"request failed for {url}: {exc}") from exc


def _validate_public_url(url: str, *, allow_http: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AcceptanceFailure("public URL must be an absolute http(s) URL")
    if parsed.scheme != "https" and not allow_http:
        raise AcceptanceFailure("public validation requires HTTPS")
    if not allow_http and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        raise AcceptanceFailure("localhost is only valid with --allow-http")


def public_results(args: argparse.Namespace) -> list[Check]:
    if not args.url:
        return [
            check(
                "public.endpoint",
                DEFERRED,
                "supply --url for the public/tunnel validation",
            )
        ]
    try:
        _validate_public_url(args.url, allow_http=args.allow_http)
    except AcceptanceFailure as exc:
        return [check("public.endpoint", FAIL, str(exc))]

    parsed = urlparse(args.url)
    host_evidence = f"host={parsed.hostname}"
    results: list[Check] = []
    try:
        frontend_status, frontend_final, _ = _http_get(args.url, args.timeout)
        if not 200 <= frontend_status < 400:
            results.append(
                check(
                    "public.frontend",
                    FAIL,
                    f"frontend returned HTTP {frontend_status}",
                    (host_evidence,),
                )
            )
        else:
            results.append(
                check(
                    "public.frontend",
                    PASS,
                    f"frontend returned HTTP {frontend_status}",
                    (host_evidence, f"final_url={urlparse(frontend_final).hostname}"),
                )
            )
    except AcceptanceFailure as exc:
        results.append(check("public.frontend", FAIL, str(exc), (host_evidence,)))

    api_url = args.api_url or urljoin(args.url.rstrip("/") + "/", "api/health")
    try:
        _validate_public_url(api_url, allow_http=args.allow_http)
        if urlparse(api_url).hostname != parsed.hostname:
            raise AcceptanceFailure(
                "frontend and API checks must use the same public/tunnel hostname"
            )
        api_status, api_final, body = _http_get(api_url, args.timeout)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcceptanceFailure(f"health endpoint did not return JSON: {exc}") from exc
        if api_status != 200 or payload.get("status") != "ok":
            raise AcceptanceFailure(
                f"health endpoint returned HTTP {api_status} and payload status "
                f"{payload.get('status')!r}"
            )
        results.append(
            check(
                "public.api",
                PASS,
                "public API health endpoint returned status=ok",
                (
                    f"final_url={urlparse(api_final).hostname}",
                    f"service={payload.get('service', 'unknown')}",
                ),
            )
        )
    except AcceptanceFailure as exc:
        results.append(check("public.api", FAIL, str(exc)))
    return results


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise AcceptanceFailure(f"invalid env line {line_number} in {path}")
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("'\"")
    return values


def _runner_online_check(repo_url: str, expected_label: str) -> Check:
    parsed = urlparse(repo_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname != "github.com" or len(parts) != 2:
        return check("runner.online", FAIL, "RUNNER_REPO_URL is not a GitHub repository URL")
    if shutil.which("gh") is None:
        return check("runner.online", DEFERRED, "gh is required for the online runner check")
    repo = "/".join(parts)
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/runners"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return check("runner.online", DEFERRED, f"could not run gh api: {exc}")
    if result.returncode != 0:
        return check(
            "runner.online",
            DEFERRED,
            "gh api could not query runners; verify GitHub authentication and repository access",
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return check("runner.online", FAIL, "gh api returned invalid runner JSON")
    runners = payload.get("runners", [])
    for runner in runners:
        labels = {label.get("name") for label in runner.get("labels", [])}
        if runner.get("status") == "online" and expected_label in labels:
            return check("runner.online", PASS, "a runner with the required label is online")
    return check(
        "runner.online",
        FAIL,
        f"no online runner with label {expected_label!r} was found",
    )


def runner_results(args: argparse.Namespace) -> list[Check]:
    compose_file = Path(args.compose_file)
    if not compose_file.is_file():
        return [check("runner.config", FAIL, f"missing runner compose file: {compose_file}")]
    try:
        compose_text = compose_file.read_text(encoding="utf-8")
    except OSError as exc:
        return [check("runner.config", FAIL, f"could not read runner compose file: {exc}")]

    required_fragments = {
        "/var/run/docker.sock": "Docker socket mount",
        "restart: unless-stopped": "restart policy",
        "indian-trading-agent-runner-config": "persistent runner config volume",
        "indian-trading-agent-runner-work": "persistent runner work volume",
        "trading-agent-prod": "required runner label",
    }
    missing = [label for fragment, label in required_fragments.items() if fragment not in compose_text]
    results = [
        check(
            "runner.config",
            FAIL if missing else PASS,
            "runner compose contains recovery-critical mounts, volumes, label, and restart policy"
            if not missing
            else f"runner compose is missing: {', '.join(missing)}",
        )
    ]

    env_name = args.runner_env or os.environ.get("TRADING_AGENT_RUNNER_ENV_FILE")
    if not env_name:
        results.append(
            check(
                "runner.credentials",
                DEFERRED,
                "runner env file is intentionally not inferred; supply --runner-env",
            )
        )
    else:
        env_path = Path(env_name).expanduser()
        if not env_path.is_file():
            results.append(
                check(
                    "runner.credentials",
                    DEFERRED,
                    f"runner env file is missing: {env_path}; no file was created",
                )
            )
        else:
            try:
                values = parse_env_file(env_path)
            except (AcceptanceFailure, OSError) as exc:
                results.append(check("runner.credentials", FAIL, str(exc)))
            else:
                missing_values = [
                    name for name in ("RUNNER_REPO_URL", "RUNNER_TOKEN") if not values.get(name)
                ]
                placeholder = values.get("RUNNER_TOKEN", "").lower().startswith("replace-with")
                if missing_values:
                    results.append(
                        check(
                            "runner.credentials",
                            DEFERRED,
                            "runner registration values are incomplete; a fresh token is "
                            "needed only when the persistent registration is absent",
                        )
                    )
                elif placeholder:
                    results.append(
                        check(
                            "runner.credentials",
                            DEFERRED,
                            "runner env contains the example token placeholder; no secret was printed",
                        )
                    )
                elif not values["RUNNER_REPO_URL"].startswith("https://github.com/"):
                    results.append(
                        check(
                            "runner.credentials",
                            FAIL,
                            "RUNNER_REPO_URL must point to an HTTPS GitHub repository",
                        )
                    )
                else:
                    results.append(
                        check(
                            "runner.credentials",
                            PASS,
                            "runner repository and token are present without printing secrets",
                        )
                    )
                if args.runner_online and values.get("RUNNER_REPO_URL"):
                    results.append(
                        _runner_online_check(
                            values["RUNNER_REPO_URL"],
                            values.get("RUNNER_LABELS", "trading-agent-prod"),
                        )
                    )
    return results


def rollback_results(args: argparse.Namespace) -> list[Check]:
    if not args.current_image or not args.previous_image:
        return [
            check(
                "rollback.dry_run",
                DEFERRED,
                "supply both current and previous digest-pinned images",
            )
        ]
    if not GHCR_DIGEST_RE.fullmatch(args.current_image):
        return [check("rollback.dry_run", FAIL, "current image is not a GHCR digest reference")]
    if not GHCR_DIGEST_RE.fullmatch(args.previous_image):
        return [check("rollback.dry_run", FAIL, "previous image is not a GHCR digest reference")]
    if args.current_image == args.previous_image:
        return [check("rollback.dry_run", FAIL, "current and previous digests are identical")]
    if args.compose_file and not Path(args.compose_file).is_file():
        return [check("rollback.dry_run", FAIL, "rollback compose file does not exist")]
    return [
        check(
            "rollback.dry_run",
            PASS,
            "rollback plan selects a different immutable digest; no compose or host command was run",
            ("next evidence: render the disposable compose fixture, then verify frontend and API health",),
        )
    ]


def _safe_compose_env(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "TRADING_AGENT_DEV_ENV_FILE": str(root / ".env.example"),
            "TRADING_AGENT_PROD_ENV_FILE": str(root / ".env.example"),
            "TRADING_AGENT_PROD_DATA_DIR": "/tmp/phase10-disposable-data",
            "CLOUDFLARED_CONFIG_FILE": str(root / "deploy/cloudflared/prod-config.yml"),
            "CLOUDFLARED_CREDENTIALS_FILE": str(root / "deploy/runner.env.example"),
            "TRADING_AGENT_RUNNER_ENV_FILE": str(root / "deploy/runner.env.example"),
        }
    )
    return environment


def _run_readonly_command(command: list[str], root: Path, environment: dict[str, str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    detail = (result.stderr or result.stdout).strip().splitlines()
    return result.returncode, detail[-1] if detail else ""


def compose_results(args: argparse.Namespace) -> list[Check]:
    if shutil.which("docker") is None:
        return [check("compose.config", DEFERRED, "docker is not installed")]
    root = Path(args.repo_root).resolve()
    compose_files = [
        root / "deploy/docker-compose.dev.yml",
        root / "deploy/docker-compose.prod.yml",
        root / "deploy/docker-compose.runner.yml",
    ]
    missing = [str(path) for path in compose_files if not path.is_file()]
    if missing:
        return [check("compose.config", FAIL, f"missing Compose file(s): {', '.join(missing)}")]

    results: list[Check] = []
    environment = _safe_compose_env(root)
    commands = [
        (
            "compose.dev",
            ["docker", "compose", "-f", str(compose_files[0]), "config", "--quiet"],
            environment,
        ),
        (
            "compose.prod",
            ["docker", "compose", "-f", str(compose_files[1]), "config", "--quiet"],
            environment,
        ),
        (
            "compose.runner",
            [
                "docker",
                "compose",
                "--env-file",
                str(root / "deploy/runner.env.example"),
                "-f",
                str(compose_files[2]),
                "config",
                "--quiet",
            ],
            environment,
        ),
    ]
    for name, command, command_environment in commands:
        return_code, detail = _run_readonly_command(command, root, command_environment)
        results.append(
            check(
                name,
                PASS if return_code == 0 else FAIL,
                "Compose config rendered without starting services"
                if return_code == 0
                else f"Compose config failed: {detail or 'unknown error'}",
            )
        )
    return results


def preflight_results(args: argparse.Namespace) -> list[Check]:
    root = Path(args.repo_root).resolve()
    required = [
        root / ".github/workflows/deploy-prod.yml",
        root / "deploy/docker-compose.dev.yml",
        root / "deploy/docker-compose.prod.yml",
        root / "deploy/docker-compose.runner.yml",
        root / "deploy/compose.env.example",
        root / "deploy/runner.env.example",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    results = [
        check(
            "acceptance.repository",
            FAIL if missing else PASS,
            "origin/main deployment inputs are present"
            if not missing
            else f"missing tracked input(s): {', '.join(missing)}",
        )
    ]
    if shutil.which("python3") is None:
        results.append(check("acceptance.python", DEFERRED, "python3 is required"))
    else:
        results.append(check("acceptance.python", PASS, "python3 is available"))
    if shutil.which("docker") is None:
        results.append(check("acceptance.docker", DEFERRED, "docker is required for Compose checks"))
    else:
        results.append(check("acceptance.docker", PASS, "docker executable is available"))
    if shutil.which("gh") is None:
        results.append(
            check(
                "acceptance.github",
                DEFERRED,
                "gh is required for PR checks and optional runner status",
            )
        )
    else:
        results.append(check("acceptance.github", PASS, "gh executable is available"))

    try:
        refs = compose_image_refs(root / "deploy/docker-compose.prod.yml")
    except OSError as exc:
        results.append(check("acceptance.provenance", FAIL, f"could not inspect production compose: {exc}"))
    else:
        if refs and not all(IMAGE_DIGEST_RE.fullmatch(ref) for ref in refs):
            results.append(
                check(
                    "acceptance.provenance",
                    DEFERRED,
                    "origin/main still uses local/tagged production images; GHCR digest promotion belongs to the unmerged artifact phase",
                )
            )
        else:
            results.append(check("acceptance.provenance", PASS, "production images are digest-pinned"))
    return results


def operator_results(args: argparse.Namespace) -> list[Check]:
    results = preflight_results(args)
    results.extend(compose_results(args))
    results.extend(provenance_results(args))
    results.extend(
        run_archive_check(
            argparse.Namespace(
                archive=args.archive,
                s3_endpoint=args.s3_endpoint,
                bucket=args.bucket,
                key=args.key,
                max_age_hours=args.max_age_hours,
                apply=False,
                i_understand_disposable=False,
                target_dir=None,
                allow_existing_disposable=False,
            ),
            restore=False,
        )
    )
    results.extend(
        public_results(
            argparse.Namespace(
                url=args.url,
                api_url=args.api_url,
                allow_http=args.allow_http,
                timeout=args.timeout,
            )
        )
    )
    results.extend(
        runner_results(
            argparse.Namespace(
                compose_file=str(Path(args.repo_root) / "deploy/docker-compose.runner.yml"),
                runner_env=args.runner_env,
                runner_online=args.runner_online,
            )
        )
    )
    results.extend(
        rollback_results(
            argparse.Namespace(
                current_image=args.current_image,
                previous_image=args.previous_image,
                compose_file=str(Path(args.repo_root) / "deploy/docker-compose.prod.yml"),
            )
        )
    )
    return results


def exit_code(results: Iterable[Check]) -> int:
    statuses = {result.status for result in results}
    if FAIL in statuses:
        return EXIT_FAILURE
    if DEFERRED in statuses:
        return EXIT_DEFERRED
    return 0


def emit(results: list[Check], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps([result.as_dict() for result in results], indent=2))
        return
    for result in results:
        print(f"[{result.status:<9}] {result.name}: {result.detail}")
        for evidence in result.evidence:
            print(f"             {evidence}")


def add_format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("text", "json"), default="text")


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_argument_group("backup source")
    source.add_argument("--archive", help="local backup archive; never defaults to production data")
    source.add_argument("--s3-endpoint", help="local/mock S3-compatible HTTP endpoint")
    source.add_argument("--bucket")
    source.add_argument("--key")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24.0,
        help="maximum accepted backup age (default: 24)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight", help="check tracked acceptance inputs")
    preflight.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])
    add_format_argument(preflight)

    compose = commands.add_parser("compose", help="render every Compose config without starting services")
    compose.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])
    add_format_argument(compose)

    restore = commands.add_parser("restore", help="restore and validate only a disposable fixture")
    add_source_arguments(restore)
    restore.add_argument("--apply", action="store_true", help="copy verified files to an explicit disposable target")
    restore.add_argument("--target-dir", help="absolute path containing disposable or restore")
    restore.add_argument("--i-understand-disposable", action="store_true")
    restore.add_argument("--allow-existing-disposable", action="store_true")
    add_format_argument(restore)

    backup = commands.add_parser("backup", help="check backup freshness and integrity without copying it")
    add_source_arguments(backup)
    add_format_argument(backup)

    provenance = commands.add_parser("provenance", help="check GHCR digest and attestation evidence")
    provenance.add_argument("--compose-file")
    provenance.add_argument("--image", dest="images", action="append")
    provenance.add_argument("--attestation-file")
    provenance.add_argument("--strict", action="store_true")
    add_format_argument(provenance)

    public = commands.add_parser("public", help="validate frontend and /api/health through a public URL")
    public.add_argument("--url")
    public.add_argument("--api-url")
    public.add_argument("--allow-http", action="store_true", help="only for local disposable HTTP fixtures")
    public.add_argument("--timeout", type=float, default=10.0)
    add_format_argument(public)

    runner = commands.add_parser("runner", help="check runner recovery inputs without starting the runner")
    runner.add_argument(
        "--compose-file",
        default=Path(__file__).resolve().parents[1] / "deploy/docker-compose.runner.yml",
    )
    runner.add_argument("--runner-env")
    runner.add_argument("--runner-online", action="store_true")
    add_format_argument(runner)

    rollback = commands.add_parser("rollback", help="validate a digest rollback plan without executing it")
    rollback.add_argument("--current-image")
    rollback.add_argument("--previous-image")
    rollback.add_argument("--compose-file")
    add_format_argument(rollback)

    operator = commands.add_parser("operator", help="run the complete read-only acceptance checklist")
    operator.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])
    add_source_arguments(operator)
    operator.add_argument("--url")
    operator.add_argument("--api-url")
    operator.add_argument("--allow-http", action="store_true")
    operator.add_argument("--timeout", type=float, default=10.0)
    operator.add_argument("--runner-env")
    operator.add_argument("--runner-online", action="store_true")
    operator.add_argument("--current-image")
    operator.add_argument("--previous-image")
    operator.add_argument("--attestation-file")
    operator.add_argument("--strict-provenance", action="store_true")
    operator.add_argument("--compose-file")
    operator.add_argument("--image", dest="images", action="append")
    add_format_argument(operator)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "preflight":
        results = preflight_results(args)
    elif args.command == "compose":
        results = compose_results(args)
    elif args.command == "restore":
        if args.apply and not args.target_dir:
            results = [check("restore.target", FAIL, "--apply requires --target-dir")]
        else:
            results = run_archive_check(args, restore=True)
    elif args.command == "backup":
        results = run_archive_check(args, restore=False)
    elif args.command == "provenance":
        results = provenance_results(args)
    elif args.command == "public":
        results = public_results(args)
    elif args.command == "runner":
        results = runner_results(args)
    elif args.command == "rollback":
        results = rollback_results(args)
    elif args.command == "operator":
        results = operator_results(args)
    else:
        results = [check("acceptance", FAIL, f"unknown command: {args.command}")]
    emit(results, args.format)
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
