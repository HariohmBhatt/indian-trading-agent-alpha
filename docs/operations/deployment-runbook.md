# Indian Trading Agent — Deployment and Operations Runbook

**Repository:** `HariohmBhatt/indian-trading-agent-alpha`
**Reference production URL:** [https://trade.hariohm.in](https://trade.hariohm.in)
**Reference development URL:** [http://dellg15:3000](http://dellg15:3000)
**Reference host:** `dellg15` (`/home/hariohm`)
**Repository baseline:** `73a1d2d`
**Last repository review:** 2026-08-02

This is the operational guide for the deployment files in this repository.
It does not provision or verify external state. DNS, Cloudflare Access and
tunnel objects, production data, backups, secrets, and GitHub rulesets must be
verified separately on the target environment.

See the [focused Docker reference](../docker-deployment.md) for the short
setup path and the [project README](../../README.md) for the user-facing
overview.

## 1. Executive summary

The application is a two-part Python/TypeScript web application:

- FastAPI backend for market data, analysis, WebSockets, persistence, and
  settings.
- Next.js frontend for the trading terminal.

There are two independent Docker Compose environments:

| Environment | Entry point | Docker behavior | Data |
| --- | --- | --- | --- |
| Development | `http://dellg15:3000` | Source bind mounts, hot reload, manually started | `indian-trading-agent-dev-data` |
| Production | `https://trade.hariohm.in` | Local builds from the checked-out source, internal network, auto-restart | `/home/hariohm/.tradingagents-prod` |

Production deployment is local, but triggered remotely:

```text
git push origin prod
        ↓
GitHub Actions workflow
        ↓
Containerized self-hosted runner on dellg15
        ↓
./deploy/deploy.sh
        ↓
Docker Compose builds and replaces the production stack
```

The repository does not define systemd units for the application. On a host
where Docker is enabled, `restart: unless-stopped` can restart containers that
already exist and were not intentionally stopped. The repository does not
install, disable, or verify legacy systemd services.

## 2. Production architecture

```text
Browser
  │
  │ https://trade.hariohm.in
  ▼
Cloudflare Access
  │ email one-time-PIN policy
  ▼
Cloudflare Tunnel container
  │
  ├── /api/* ───────────────► backend:8000
  │                           FastAPI + WebSockets
  │
  └── everything else ──────► frontend:3000
                              Next.js standalone server

backend:8000 ──► /data/trading_agent.db
             └──► /data/memory, /data/cache, /data/logs
```

The tracked tunnel configuration names the `trade` tunnel with ID
`989090d5-dfd5-4b87-b70a-90d6fe96ae6f`. The Cloudflare tunnel object, DNS
record, Access application, and Access policy live in the Cloudflare account;
they are not created by this repository. The credentials are never stored in
Git.

The frontend resolves its API base dynamically:

- HTTPS production: same-origin `https://trade.hariohm.in`
- Tailscale development: `http://dellg15:8000`
- local fallback: `http://localhost:8000`

This also makes analysis, scanner, and backtest WebSockets use `wss://` in
production and `ws://` in development.

## 3. Repository deployment files

| Path | Responsibility |
| --- | --- |
| `.github/workflows/deploy-prod.yml` | Runs deployment on pushes to `prod` |
| `.dockerignore` | Keeps secrets, Git metadata, local data, and build output out of images |
| `deploy/docker/backend.Dockerfile` | Python backend production/development targets |
| `deploy/docker/frontend.Dockerfile` | Next.js development target and standalone production target |
| `deploy/docker-compose.dev.yml` | Isolated local development stack |
| `deploy/docker-compose.prod.yml` | Production backend, frontend, and Cloudflare connector |
| `deploy/docker-compose.runner.yml` | Containerized GitHub Actions runner |
| `deploy/deploy.sh` | Builds, starts, and health-checks production |
| `deploy/dev.sh` | Starts/stops/rebuilds development |
| `deploy/cloudflared/prod-config.yml` | Tunnel routes to Compose service names |
| `deploy/runner/Dockerfile` | Builds the self-hosted runner image |
| `deploy/runner/entrypoint.sh` | Registers/reconnects the runner and grants Docker-socket access |
| `deploy/compose.env.example` | Template for host-only production paths |
| `deploy/runner.env.example` | Template for the host-only runner registration configuration |
| `docs/docker-deployment.md` | Focused Docker setup reference |
| `docs/operations/deployment-runbook.md` | This complete operations guide |

The old host-based systemd unit files and installer are not present in this
repository. A target host may still have legacy units installed; inspect them
before changing or disabling anything.

## 4. Development environment

Start the development Docker stack from the repository root:

```bash
./deploy/dev.sh
```

Useful commands:

```bash
./deploy/dev.sh ps
./deploy/dev.sh logs
./deploy/dev.sh rebuild
./deploy/dev.sh down
```

Development behavior:

- Frontend is exposed on host port `3000`.
- Backend is exposed on host port `8000`.
- Backend runs Uvicorn with reload enabled.
- Frontend runs Next.js development mode.
- Source directories are bind-mounted into the containers.
- `WATCHPACK_POLLING=true` makes frontend file watching reliable.
- Development state is stored in the named volume
  `indian-trading-agent-dev-data`.
- Development does not use the production database directory.
- Development containers intentionally do not auto-start at boot.

The development Compose file defaults to the repository `.env` file. It can be
overridden without editing the file:

```bash
TRADING_AGENT_DEV_ENV_FILE=/path/to/dev.env ./deploy/dev.sh
```

## 5. Production environment

### 5.1 Host-only configuration

The real production configuration is outside the repository. The following is
the layout on the reference host, not a path that this repository creates:

```text
/home/hariohm/.config/indian-trading-agent/
├── compose.env
├── prod.env
├── cloudflared-config.yml
└── cloudflared-credentials.json
```

The tracked template is `deploy/compose.env.example`:

```dotenv
TRADING_AGENT_PROD_ENV_FILE=/home/hariohm/.config/indian-trading-agent/prod.env
TRADING_AGENT_PROD_DATA_DIR=/home/hariohm/.tradingagents-prod
CLOUDFLARED_CONFIG_FILE=/home/hariohm/.config/indian-trading-agent/cloudflared-config.yml
CLOUDFLARED_CREDENTIALS_FILE=/home/hariohm/.config/indian-trading-agent/cloudflared-credentials.json
```

The actual files contain secrets or secret-adjacent credentials and must not
be committed:

- `prod.env`: provider API keys and runtime environment settings.
- `cloudflared-credentials.json`: tunnel credentials.
- `runner.env`: GitHub runner registration token.
- `compose.env`: host paths; readable by the runner so Compose can resolve
  them.

For a fresh host, use these sources and do not treat the repository as a
secret backup:

- Copy `deploy/compose.env.example` to the host-only directory, then replace
  every reference-host path with the target host's real paths.
- Obtain `prod.env` from the approved secret manager or an access-controlled
  secret backup. `.env.example` documents variable names; a developer's `.env`
  is not the production source of truth and must not be copied blindly.
- Copy `deploy/cloudflared/prod-config.yml` for the tracked ingress
  configuration, then obtain the matching
  `cloudflared-credentials.json` from the Cloudflare account or its protected
  backup. The tunnel ID and hostname must be verified against the external
  Cloudflare tunnel.
- Restore the complete production data directory from an authorized,
  tested backup; see the persistent-state and backup notes below.
- Generate `deploy/runner.env` from the GitHub repository runner settings with
  a fresh registration token; see the runner lifecycle below.

The production environment file is group-readable by the Docker group so the
containerized runner can read it through the read-only host configuration
mount:

```bash
sudo chgrp docker ~/.config/indian-trading-agent/prod.env
sudo chmod 640 ~/.config/indian-trading-agent/prod.env
chmod 644 ~/.config/indian-trading-agent/compose.env
```

The Docker group grants root-equivalent access to the host; membership should
be limited to trusted users.

The checked-in examples are tied to the reference host and are not portable:

- `deploy/compose.env.example` contains `/home/hariohm/...` paths.
- `.github/workflows/deploy-prod.yml` passes
  `/home/hariohm/.config/indian-trading-agent/compose.env` to the deploy
  script.
- `deploy/docker-compose.runner.yml` bind-mounts the host path
  `/home/hariohm/.config/indian-trading-agent` and
  `deploy/runner.env.example` contains the reference checkout path.

Therefore, copying the configuration alone does not make a different user,
home directory, or checkout path supported. A fresh host must use the same
absolute paths or receive a separately reviewed deployment-file change. This
documentation-only phase does not make those paths portable.

### 5.2 Persistent state

The application now honors `TRADINGAGENTS_HOME` for its database, cache,
memory, and log paths. The backend also supports `TRADINGAGENTS_DB_PATH`.

Production sets:

```text
TRADINGAGENTS_HOME=/data
```

The host bind mount is:

```text
/home/hariohm/.tradingagents-prod:/data
```

The existing state from `~/.tradingagents` was copied into this production
directory during the reference-host Docker migration. That historical copy is
not a backup guarantee. Development uses a different named volume, so test
trades/settings/memories do not modify production.

The database and its surrounding state are external to Git. The Compose and
deployment files do not create backups, upload data, restore data, or perform
a transactional database rollback. The default SQLite path is
`/data/trading_agent.db`; `TRADINGAGENTS_DB_PATH` in `prod.env`, if set, can
override it.

Operational recovery may assume a copy in S3-compatible object storage, but
that is only an external assumption: this repository defines no bucket,
endpoint, prefix, credentials, retention policy, backup job, or restore
command. Before relying on such a backup, identify the approved object-store
location, access it through the normal secret-management process, and test a
full restore of the production data directory. A code rollback does not roll
back SQLite data, API-key settings, or memories.

### 5.3 Production commands

Manual deployment:

```bash
./deploy/deploy.sh
```

The script:

1. Loads host paths from
   `TRADING_AGENT_COMPOSE_ENV_FILE`, defaulting to
   `~/.config/indian-trading-agent/compose.env`.
2. Builds the backend and frontend production images.
3. Starts or replaces the production Compose services with
   `up -d --build --remove-orphans`.
4. Removes orphaned Compose services.
5. Waits for the backend and frontend health checks.
6. Prints Compose status.
7. Prints recent logs and exits nonzero if health checks fail.

These are local builds from the checked-out source, not immutable artifacts
promoted from a registry. The Compose file also uses the mutable
`cloudflare/cloudflared:latest` tag. No image digest, signature, artifact
registry, or automatic rollback is configured here.

To use a non-default host-only path when running manually:

```bash
TRADING_AGENT_COMPOSE_ENV_FILE=/path/to/compose.env ./deploy/deploy.sh
```

Inspect production:

```bash
docker compose \
  --env-file ~/.config/indian-trading-agent/compose.env \
  -f deploy/docker-compose.prod.yml ps

docker compose \
  --env-file ~/.config/indian-trading-agent/compose.env \
  -f deploy/docker-compose.prod.yml logs -f backend frontend cloudflared
```

Stop/start production manually:

```bash
docker compose \
  --env-file ~/.config/indian-trading-agent/compose.env \
  -f deploy/docker-compose.prod.yml down

./deploy/deploy.sh
```

The Compose project does not publish backend/frontend ports to the host.
Only the Cloudflare connector reaches the services over the internal
`indian-trading-agent-prod` network.

## 6. Boot and restart behavior

Docker is enabled on the host:

```bash
sudo systemctl enable --now docker
```

These services use `restart: unless-stopped`:

- production backend
- production frontend
- production Cloudflare connector
- containerized GitHub Actions runner

After a normal host reboot, Docker can restart the production site and runner
if those containers were already created and were not manually stopped.
Development remains manual. `restart: unless-stopped` does not create missing
containers, restore data, or prove that legacy host services are absent.

The repository does not manage the historical host-based units
`cloudflared.service`, `trading-agent-backend.service`,
`trading-agent-frontend.service`, `trading-agent-prod-backend.service`, or
`trading-agent-prod-frontend.service`. Inspect a target host before disabling
or enabling any of them.

## 7. GitHub Actions deployment

The workflow is `.github/workflows/deploy-prod.yml`.

Triggers:

- push to the `prod` branch
- manual `workflow_dispatch`

The job runs on:

```yaml
self-hosted
linux
x64
trading-agent-prod
```

The reference runner is configured as:

```text
dellg15-prod-runner
```

It normally runs as `indian-trading-agent-runner-runner-1`, mounts the Docker
socket, and keeps its runner configuration/work directory in named Docker
volumes. The container is not a GitHub-hosted VM; builds and deployments occur
on the host reached through that socket.

The action sequence is:

1. GitHub sends the job to `dellg15-prod-runner`.
2. `actions/checkout` checks out the exact `prod` commit.
3. The action sets
   `TRADING_AGENT_COMPOSE_ENV_FILE=/home/hariohm/.config/indian-trading-agent/compose.env`.
4. It runs `./deploy/deploy.sh`.
5. Docker builds locally and Compose health-checks the services.

The workflow has a concurrency group named
`trading-agent-production`; production deployments do not overlap.

### Runner operations

```bash
docker compose \
  --env-file deploy/runner.env \
  -f deploy/docker-compose.runner.yml ps

docker compose \
  --env-file deploy/runner.env \
  -f deploy/docker-compose.runner.yml logs -f runner

docker compose \
  --env-file deploy/runner.env \
  -f deploy/docker-compose.runner.yml up -d --build
```

`deploy/runner.env` is ignored by Git and contains a short-lived GitHub
registration token. `deploy/runner/entrypoint.sh` requires `RUNNER_TOKEN` to
be non-empty on every container start, but consumes it for registration only
when the persistent `.runner` file is absent. A normal restart can reconnect
using the persistent runner configuration; if the
`indian-trading-agent-runner-config` volume is deleted, obtain a fresh
repository registration token before restarting. Revoke and replace the
token if it is exposed. The file must remain at the path supplied by
`TRADING_AGENT_RUNNER_ENV_FILE`, and its reference-host example is not
portable.

GitHub CLI monitoring:

```bash
gh run list \
  --repo HariohmBhatt/indian-trading-agent-alpha \
  --branch prod \
  --workflow deploy-prod.yml

gh run watch RUN_ID \
  --repo HariohmBhatt/indian-trading-agent-alpha \
  --exit-status
```

The first production run completed successfully:

- Commit: `73a1d2d14e18d4cd6db0866d02d296f768246d46`
- Run: [30734946285](https://github.com/HariohmBhatt/indian-trading-agent-alpha/actions/runs/30734946285)
- Result: success
- Duration: approximately 1 minute 47 seconds

## 8. Branch governance and access control

The repository is public. Branch protection and rulesets are GitHub-side
configuration; no file in this repository enforces them. At the last external
verification, `prod` had an owner-only ruleset
[Personal-only prod](https://github.com/HariohmBhatt/indian-trading-agent-alpha/rules/20222418)
that blocked deletion and non-fast-forward updates. Ruleset IDs, bypass
actors, and enforcement can change, so verify the current rules for both
`main` and `prod` before every release.

Recommended release flow:

```bash
git status --short
git fetch origin main prod
git switch main
git pull --ff-only origin main
# review and test the exact main commit

git switch prod
git pull --ff-only origin prod
git merge --ff-only main
git push origin prod

git switch main
```

The `prod` push is the production release action: it causes the workflow to
check out that `prod` commit and run `./deploy/deploy.sh`. The fast-forward
requirement prevents an unreviewed divergent local `prod` history from being
published. Do not add a `pull_request`-triggered workflow that executes on
this self-hosted runner; untrusted public pull-request code must not receive
access to the Docker socket or production host files.

### Rollback

There is no automatic or transactional rollback. `deploy.sh` builds in place
and Compose may replace the running containers before its health check fails;
the previous application image is not retained as a pinned release artifact.
A code rollback also does not restore the external SQLite/data directory.

Use a forward commit so the protected `prod` branch remains fast-forwardable.
For one bad, non-merge release commit:

```bash
git status --short
git fetch origin prod
git switch prod
git pull --ff-only origin prod
git revert --no-edit BAD_RELEASE_COMMIT
git push origin prod
```

For a release containing several commits or merge commits, restore the
tracked tree to a reviewed known-good commit, inspect the resulting diff, and
publish that restore as a new commit:

```bash
git status --short
git fetch origin prod
git switch prod
git pull --ff-only origin prod
git restore --source=GOOD_COMMIT --staged --worktree -- .
git diff --cached --stat
git commit -m "revert: restore production to GOOD_COMMIT"
git push origin prod
```

Monitor the resulting workflow before declaring recovery. If the incident
involves corrupted or incompatible application data, use the separately
verified data backup process; Git history cannot roll back that state.

## 9. Cloudflare deployment and access

DNS for `trade.hariohm.in`, the tunnel object, and the previous DNS provider
state are external Cloudflare configuration. The repository does not create,
update, or verify those records.

Cloudflare Tunnel ingress:

```text
/api/* → http://backend:8000
/*      → http://frontend:3000
```

Cloudflare Access is expected to protect the entire `trade.hariohm.in`
application with an email one-time-PIN policy on the reference environment.
The Access application and policy are configured in the Cloudflare Zero Trust
dashboard and are not represented in this repository. A fresh host and a
copied tunnel credential do not recreate that access control.

If the external DNS, tunnel, and Access state are present, an unauthenticated
check should look like:

```bash
curl -I https://trade.hariohm.in
# HTTP 302 to the Cloudflare Access login page
```

Expected authenticated behavior is the trading terminal. Do not remove or
assume the Access policy while API keys or broker credentials are reachable
through the Settings UI.

## 10. Security model

Repository-level safeguards and boundaries:

- `.env`, runner tokens, tunnel credentials, databases, and local data are
  ignored by Git.
- Production secrets are mounted from outside the repository.
- Production services are not directly exposed on host ports.
- Development and production data are separate.
- The production workflow is not triggered by pull requests.
- The Cloudflare config/credential and runner configuration bind mounts are
  read-only.

The following are external prerequisites, not guarantees provided by this
repository:

- Cloudflare Access must be enabled to prevent anonymous public use.
- GitHub rulesets must restrict who can update `main` and `prod`, and whether
  force-pushes or branch deletion are allowed.
- The production data and secret backups must be available and tested.

Important residual trust boundary:

The self-hosted runner mounts the raw `/var/run/docker.sock`. Its entrypoint
adds the runner user to the host socket's group, so a job running on this
runner can use the Docker API with effectively root-level control of the
host, including access beyond the production Compose project. This is not
limited to "creating production containers" and is not mitigated by the
read-only configuration mounts. Only owner-approved release commits should
reach `prod`; review every release commit before pushing it. Do not run
untrusted pull-request code on this runner.

The application still handles third-party LLM, market-data, and broker
credentials. Keep the Cloudflare Access policy enabled and rotate credentials
if the host, GitHub account, runner token, or Docker socket is ever exposed.

## 11. Verification performed

The following checks passed during the earlier Docker implementation:

- Production backend Docker image build.
- Production frontend Docker image build.
- Development backend/frontend Docker image build.
- Production Compose startup and health checks.
- Development Compose startup and health checks.
- Cloudflare Tunnel container connectivity pre-checks.
- The reference production URL reached Cloudflare Access.
- The reference development URL returned HTTP 200.
- Backend test suite: `12 passed`.
- GitHub Actions production deployment: successful.
- Self-hosted runner status: online.
- Docker daemon: enabled and active.
- All production and runner containers: `unless-stopped`.

`npm run lint` reported pre-existing frontend lint violations in unrelated
application pages. The production Next.js build and TypeScript build
succeeded. The successful GitHub Actions run also emitted a non-blocking
annotation that `actions/checkout@v4` targets deprecated Node.js 20 and is
being forced to run on Node.js 24. These are historical results, not evidence
that a fresh host or this documentation-only PR has passed those checks.

## 12. Troubleshooting

### Production page is unavailable

```bash
docker compose \
  --env-file ~/.config/indian-trading-agent/compose.env \
  -f deploy/docker-compose.prod.yml ps

docker compose \
  --env-file ~/.config/indian-trading-agent/compose.env \
  -f deploy/docker-compose.prod.yml logs --tail=100 backend frontend cloudflared
```

Then retry:

```bash
./deploy/deploy.sh
```

### Cloudflare Tunnel is restarting

Check the connector logs and host-only files:

```bash
docker compose \
  --env-file ~/.config/indian-trading-agent/compose.env \
  -f deploy/docker-compose.prod.yml logs cloudflared

ls -l ~/.config/indian-trading-agent/cloudflared-config.yml \
  ~/.config/indian-trading-agent/cloudflared-credentials.json
```

The tunnel credential is mounted at
`/etc/cloudflared/credentials.json` inside the container. The service runs as
container root because the credential file is intentionally mode `600`.

### GitHub runner is offline

```bash
docker compose \
  --env-file deploy/runner.env \
  -f deploy/docker-compose.runner.yml ps

docker compose \
  --env-file deploy/runner.env \
  -f deploy/docker-compose.runner.yml logs runner

gh api repos/HariohmBhatt/indian-trading-agent-alpha/actions/runners \
  --jq '.runners[] | [.name,.status,.busy] | @tsv'
```

Restart it:

```bash
docker compose \
  --env-file deploy/runner.env \
  -f deploy/docker-compose.runner.yml up -d --build
```

If the runner registration volume was removed, generate a new repository
runner token and recreate `deploy/runner.env`. The token must be non-empty
even when the persistent registration is present because the entrypoint checks
it on every start.

### A deployment fails

The deployment script reports recent container logs and returns a nonzero
status, which fails the GitHub job. The Compose replacement is not
transactional; it does not provide an automatic rollback. Follow the
forward-only rollback procedure in [Rollback](#rollback); do not assume that
the failed deployment left the previous application image available.

### Docker permission denied

The deploying user must be in the Docker group:

```bash
sudo usermod -aG docker "$USER"
```

Log out and back in before using Docker without `sudo`.

## 13. Rebuilding the host from scratch

This repository is not a self-contained host image. A rebuild requires all of
the following, in addition to an installed Docker Engine and Compose plugin:

1. Clone the repository at the reviewed `prod` commit. Use the reference
   absolute paths or make the separately reviewed deployment-file changes
   required by the hard-coded path notes in section 5.1.
2. Restore `compose.env`, `prod.env`, the tracked Cloudflare config, and the
   external Cloudflare credentials using the provenance in section 5.1.
3. Restore the complete `/home/hariohm/.tradingagents-prod` equivalent from an
   authorized, tested backup. An S3-compatible backup is an external
   operational assumption, not a feature configured by this repository.
4. Verify the external Cloudflare tunnel, DNS, and Access policy before
   exposing the host.
5. Enable Docker:
   `sudo systemctl enable --now docker`.
6. Start production from the repository root:
   `./deploy/deploy.sh`.
7. Create `deploy/runner.env` from `deploy/runner.env.example`, set the
   checkout path in `TRADING_AGENT_RUNNER_ENV_FILE`, and provide a fresh
   runner registration token.
8. Start the runner from the repository root:

   ```bash
   docker compose \
     --env-file deploy/runner.env \
     -f deploy/docker-compose.runner.yml up -d --build
   ```

9. Confirm the runner is online with the `trading-agent-prod` label and
   confirm current GitHub rulesets.
10. Push a controlled commit to `prod` only after the preceding checks, then
    monitor the workflow.

Never restore secrets by committing them to the repository.

## 14. Current implementation record

The work completed in this project followed these historical milestones:

1. The existing FastAPI/Next.js application was identified as the deployable
   stack: backend `:8000`, frontend `:3000`, SQLite/local memory persistence,
   and WebSocket streaming.
2. Public access was configured with Cloudflare Tunnel and
   `trade.hariohm.in`; DNS state remains external to this repository.
3. The frontend API client was changed to use same-origin `/api` traffic over
   HTTPS, preserving the Tailscale `dellg15:8000` development behavior.
4. Cloudflare Access email one-time-PIN protection was enabled for the public
   site in the external Cloudflare account.
5. A temporary host-based production split was created with a separate
   checkout and ports `8100/3100` while development used `3000/8000`.
6. The host-based services were replaced with separate Docker development and
   production stacks, isolated persistence, and a Dockerized Cloudflare
   connector.
7. A Dockerized GitHub Actions runner was registered on `dellg15`, and the
   `prod` branch workflow was tested successfully.
8. An owner-only repository ruleset was applied to the public `prod` branch;
   current ruleset state remains external to this repository.

The Docker conversion was committed and published as:

```text
73a1d2d ci: containerize dev and production deployment
```

The earlier application/prod split work was committed as:

```text
17b410a feat(portfolio): add positions tracking; add prod deploy pipeline
```

The current reference runtime is the Docker architecture described above. The
older `/home/hariohm/indian-trading-agent-prod` checkout may remain on the
reference host as a legacy artifact, but this repository does not inspect or
remove it.
