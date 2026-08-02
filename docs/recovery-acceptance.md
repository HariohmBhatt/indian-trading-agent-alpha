# Recovery and release acceptance

This document is the Phase 10 operator gate for the deployment remediation
work. It is an acceptance/runbook change only.

The branch is based on `origin/main`. The earlier remediation phases are not
merged into this branch, so this runbook never assumes that their workflows,
backup jobs, immutable image pipeline, or readiness endpoints exist. Checks
that need those inputs report `DEFERRED` with the missing prerequisite.

The acceptance tooling is deliberately read-only by default:

```bash
./scripts/phase10-recovery-acceptance.sh
```

It does not call `docker compose up`, push an image, change a GitHub runner,
contact a production host, or alter production data. The wrapper rejects
restore-target arguments. A restore copy is available only through the
explicit `restore --apply` command, and that command accepts only a path
named for a disposable or restore fixture.

## Statuses and exit codes

| Status | Meaning |
| --- | --- |
| `PASS` | The supplied fixture or read-only configuration met the check. |
| `FAIL` | A supplied input is unsafe, invalid, stale, or inconsistent. |
| `DEFERRED` | An operator-controlled prerequisite was not supplied or is not present on this base branch. |

The checker exits `0` when every requested check passes, `1` when any check
fails, and `2` when there are no failures but at least one check is deferred.
The deferred exit is intentional: it prevents an incomplete release from
being reported as accepted.

## Operator acceptance criteria

Record evidence for every row before approving a production release.

| Gate | Acceptance criterion | Evidence |
| --- | --- | --- |
| Release scope | The candidate commit is the intended `main` release and every prerequisite remediation PR is merged. | Merge-base output and links to the merged PRs. |
| Configuration | Development, production, and runner Compose files render without starting services. | `recovery_acceptance.py compose` output. |
| Image provenance | Application images are GHCR `@sha256:` references; auxiliary images use an approved immutable registry digest; every promoted digest is covered by signed provenance/SBOM evidence. | Registry inspection and recorded `cosign` verification JSON. |
| Backup freshness | The newest successful backup is no older than 24 hours and its manifest/checksums are valid. | Backup job timestamp, object key, and `backup` output. |
| Restore | The backup restores to a disposable host, SQLite passes `PRAGMA integrity_check`, and the disposable application reaches its readiness endpoints. | Restore log, disposable host ID, and health responses. |
| Public path | HTTPS reaches the frontend and `/api/health` through the configured tunnel; origin application ports are not exposed publicly. | `public` output plus tunnel and firewall evidence. |
| Runner | The self-hosted runner is online with the required label, its registration/work volumes survive restart, and the recovery procedure has been rehearsed. | `gh api` runner output and disposable restart evidence. |
| Rollback | A previously accepted digest is available and a rollback render/health dry run succeeds on a disposable stack. | `rollback` output and disposable Compose evidence. |
| Sign-off | The operator records RTO/RPO observations and explicitly lists any deferred prerequisite. | Completed checklist attached to the release/change record. |

No row is accepted merely because its command was not run. A missing secret,
endpoint, image digest, runner registration, or prerequisite PR is a
`DEFERRED` result.

## RTO and RPO targets

These are the Phase 10 operational targets. The service owner may tighten them
in the change record, but must not silently relax them:

| Metric | Target | Measurement |
| --- | --- | --- |
| RPO | At most 24 hours of application state | UTC timestamp in the newest valid backup manifest at incident start. |
| RTO | A healthy disposable replacement within 60 minutes | Elapsed time from restore approval to both frontend and API health checks passing. |

The RPO target is a freshness gate, not a claim that every external market
provider or broker state is recoverable. The restore evidence must identify
which application state is covered and which external state must be
reconciled manually. The RTO clock includes prerequisite discovery and
validation; it does not start after a failed restore has already been hidden.

## Disposable restore drill

The acceptance fixture is a tar archive containing a root `manifest.json`, a
SQLite database, and secret-free application data. The manifest format is:

```json
{
  "schema_version": 1,
  "created_at": "2026-08-02T06:30:00+00:00",
  "database": "data/trading_agent.db",
  "files": [
    {
      "path": "data/trading_agent.db",
      "size": 12345,
      "sha256": "64-character-hex-digest"
    }
  ]
}
```

Create a fixture without using production data:

```bash
fixture_dir="$(mktemp -d)"
python3 scripts/create_disposable_backup_fixture.py \
  --output "$fixture_dir/fixtures/backup.tar.gz"
```

Run the restore check against a local S3-compatible HTTP endpoint. The
endpoint below is a disposable object server, not a cloud bucket:

```bash
python3 -m http.server 18080 --bind 127.0.0.1 --directory "$fixture_dir" &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true; rm -rf "$fixture_dir"' EXIT

python3 scripts/recovery_acceptance.py restore \
  --s3-endpoint http://127.0.0.1:18080 \
  --bucket fixtures \
  --key backup.tar.gz
```

This dry run downloads into a temporary directory, rejects archive traversal
and links, verifies every manifest checksum, enforces the 24-hour freshness
limit, and runs SQLite integrity checking. It does not create a restore target.

To rehearse the copy step, use a new disposable path and opt in explicitly:

```bash
python3 scripts/recovery_acceptance.py restore \
  --archive "$fixture_dir/fixtures/backup.tar.gz" \
  --apply \
  --target-dir /tmp/indian-trading-agent-disposable-restore \
  --i-understand-disposable
```

The checker refuses the known production data directories, non-absolute paths,
non-empty targets unless explicitly allowed, and target names that do not
contain `disposable` or `restore`. It never reads a production path by
default. After the copy, the operator must start a disposable stack with
fixture-only environment values and record frontend/API health separately.

For a real backup source, pass its object through an authenticated,
operator-controlled S3-compatible endpoint. Do not put access keys in this
repository, fixture, command line, or acceptance output.

## Backup freshness check

The freshness-only form is useful in a scheduled acceptance check:

```bash
python3 scripts/recovery_acceptance.py backup \
  --s3-endpoint "$DISPOSABLE_S3_ENDPOINT" \
  --bucket "$BACKUP_BUCKET" \
  --key "$BACKUP_KEY" \
  --max-age-hours 24
```

The check requires an ISO-8601 `created_at` (or `backup_at`) value, validates
all listed SHA-256 values, and requires a declared SQLite database. A stale
backup is `FAIL`; an absent endpoint, bucket, or key is `DEFERRED`.

## GHCR digest promotion

The current `origin/main` production Compose file uses local/tagged image
defaults, so the provenance check correctly reports the artifact phase as
deferred on this branch:

```bash
python3 scripts/recovery_acceptance.py provenance \
  --compose-file deploy/docker-compose.prod.yml
```

Once the prerequisite immutable-artifact changes are merged, the operator
procedure is:

1. Build from the reviewed commit SHA with BuildKit provenance and SBOM
   generation enabled. Push a commit-specific staging tag to GHCR.
2. Resolve the registry digest with `docker buildx imagetools inspect`. Record
   the digest; never copy a tag into the production environment as the release
   identity.
3. Verify the SLSA provenance and SBOM with the repository's approved `cosign`
   identity/issuer policy. Save the verification JSON as release evidence.
4. Promote the exact digest to any human-readable release tag if desired.
   Promotion must point at the digest, not rebuild the image.
5. Set each application image variable to
   `ghcr.io/<owner>/<image>@sha256:<64-hex-digest>` and pin any auxiliary
   image to its approved registry digest. Render Compose and perform the
   disposable health/rollback rehearsal.

Validate the candidate without pushing or retagging:

```bash
python3 scripts/recovery_acceptance.py provenance \
  --image ghcr.io/<owner>/indian-trading-agent-backend@sha256:<digest> \
  --image ghcr.io/<owner>/indian-trading-agent-frontend@sha256:<digest> \
  --attestation-file /path/to/recorded-provenance.json \
  --strict
```

Mutable GHCR tags are a failure. Local/tagged images on this base branch are
reported as deferred because the artifact prerequisite is not present here.
The acceptance tools never run `docker push`, `imagetools create`, `cosign`,
or a production Compose command.

## Public and tunnel validation

Run this only after the public hostname is deliberately pointed at the
disposable or approved release:

```bash
python3 scripts/recovery_acceptance.py public \
  --url https://trade.example.invalid \
  --api-url https://trade.example.invalid/api/health
```

The check requires HTTPS, accepts a successful frontend response, and requires
HTTP 200 with JSON `{"status":"ok"}` from the API health endpoint. Use
`--allow-http` only for a loopback fixture; never use it as production
evidence. Also record that host ports `3000` and `8000` are not publicly
reachable and that the tunnel ingress routes `/api` to the backend and all
other application paths to the frontend.

## Runner recovery

The runner check inspects the tracked Compose file without starting it:

```bash
python3 scripts/recovery_acceptance.py runner \
  --runner-env /path/to/disposable/runner.env
```

It checks the Docker socket mount, required label, restart policy, and
persistent configuration/work volumes. The env file is never inferred from a
production location and token values are never printed. If registration state
is absent, create a fresh short-lived GitHub runner token; do not reuse a
printed or committed token. To query GitHub status explicitly:

```bash
python3 scripts/recovery_acceptance.py runner \
  --runner-env /path/to/runner.env \
  --runner-online
```

The recovery rehearsal is:

1. Capture the runner label/name and confirm the runner is idle.
2. Stop and start the disposable runner Compose project.
3. Confirm the persistent `.runner` registration and work volume survive.
4. Run one harmless workflow job that performs no deployment.
5. If registration is lost, remove only the disposable registration, issue a
   fresh token, register once, and repeat the harmless job.

The production recovery command is an operator action requiring the normal
change approval; this PR supplies the checklist and read-only checks only.

## Rollback dry run

Both rollback references must be immutable and different:

```bash
python3 scripts/recovery_acceptance.py rollback \
  --current-image ghcr.io/<owner>/backend@sha256:<current-digest> \
  --previous-image ghcr.io/<owner>/backend@sha256:<known-good-digest> \
  --compose-file deploy/docker-compose.prod.yml
```

The command validates the plan and prints no executable deployment command.
On a disposable stack, render the Compose file with the previous digest,
start only that disposable stack, verify frontend/API health, and record the
elapsed time. A production rollback must use the same previously accepted
digest and the normal release approval path.

## Final sign-off

- [ ] Candidate commit and all prerequisite remediation PRs are identified.
- [ ] `preflight` and all three Compose config checks are recorded.
- [ ] Backup freshness is within the RPO target.
- [ ] Disposable restore and SQLite integrity evidence is attached.
- [ ] Disposable frontend/API health evidence is attached.
- [ ] GHCR digest and provenance evidence is attached, or the gate is marked
      deferred pending the immutable-artifact prerequisite.
- [ ] Public/tunnel HTTPS evidence and origin exposure evidence are attached.
- [ ] Runner restart/recovery evidence is attached.
- [ ] Rollback dry-run evidence is attached.
- [ ] RTO/RPO observations and all deferred checks are recorded.

Phase 10 does not merge prerequisite branches, modify production Compose,
deploy, enable auto-merge, or claim that a deferred check passed.
