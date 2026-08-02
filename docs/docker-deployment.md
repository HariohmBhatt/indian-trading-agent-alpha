# Docker deployment

The repository has two isolated Docker Compose projects:

- `deploy/docker-compose.dev.yml` — local development at `http://dellg15:3000`
- `deploy/docker-compose.prod.yml` — production behind the Cloudflare Tunnel at `https://trade.hariohm.in`

Both projects build the same source with different Docker targets. Development
bind-mounts source code and runs Next.js/FastAPI reloaders. Production runs
immutable application images, separate persistent data, and `restart:
unless-stopped`.

## Local development

```bash
./deploy/dev.sh
```

The development stack uses host ports `3000` and `8000`, plus the named volume
`indian-trading-agent-dev-data`. It does not share the production database.

```bash
./deploy/dev.sh logs
./deploy/dev.sh down
```

## Production host setup

Create the host-only configuration directory and copy the current runtime
secrets/data into it:

```bash
mkdir -p ~/.config/indian-trading-agent
touch ~/.config/indian-trading-agent/deploy.lock
cp deploy/compose.env.example ~/.config/indian-trading-agent/compose.env
cp .env ~/.config/indian-trading-agent/prod.env
cp deploy/cloudflared/prod-config.yml \
  ~/.config/indian-trading-agent/cloudflared-config.yml
cp ~/.cloudflared/989090d5-dfd5-4b87-b70a-90d6fe96ae6f.json \
  ~/.config/indian-trading-agent/cloudflared-credentials.json
chmod 600 ~/.config/indian-trading-agent/prod.env \
  ~/.config/indian-trading-agent/cloudflared-credentials.json
chmod 644 ~/.config/indian-trading-agent/compose.env
chmod 664 ~/.config/indian-trading-agent/deploy.lock
sudo chgrp docker ~/.config/indian-trading-agent/prod.env
sudo chmod 640 ~/.config/indian-trading-agent/prod.env
```

The production data directory in `compose.env` should contain the existing
production database. The setup script can seed it once:

```bash
mkdir -p ~/.tradingagents-prod
rsync -a ~/.tradingagents/ ~/.tradingagents-prod/
```

Then deploy manually:

```bash
TRADING_AGENT_EXPECTED_REF=refs/heads/prod \
TRADING_AGENT_EXPECTED_SHA="$(git rev-parse HEAD)" \
./deploy/deploy.sh
```

The deploy script validates that the checked-out commit is on `prod`, checks
Docker/Compose and every host path referenced by `compose.env`, validates
`docker compose config`, and holds `deploy.lock` for the complete operation.
Use `./deploy/deploy.sh --dry-run` with the same two revision variables to run
all validation without starting or replacing containers.

The Cloudflare Tunnel is a container in the production Compose project. Stop
the old host `cloudflared` service before starting this stack so only the
Docker connector is active:

```bash
sudo systemctl disable --now cloudflared
./deploy/deploy.sh
```

The same applies to the old host-based application services:

```bash
sudo systemctl disable --now trading-agent-backend trading-agent-frontend \
  trading-agent-prod-backend trading-agent-prod-frontend
```

Docker itself must be enabled at boot:

```bash
sudo systemctl enable --now docker
```

The production containers use `restart: unless-stopped`, so Docker brings them
back after a reboot. The development stack is intentionally not configured to
start automatically.

## GitHub Actions deployment

The workflow `.github/workflows/deploy-prod.yml` runs on every push to the
`prod` branch. It uses a repository-level self-hosted runner on `dellg15`, so
the build and deployment happen on the local machine. Nothing is deployed to
GitHub-hosted infrastructure.

The runner itself is Dockerized and uses the host Docker socket only to create
the production containers:

```bash
cp deploy/runner.env.example deploy/runner.env
# Replace RUNNER_TOKEN with a fresh token from:
# GitHub repository → Settings → Actions → Runners → New self-hosted runner

docker compose \
  --env-file deploy/runner.env \
  -f deploy/docker-compose.runner.yml \
  up -d --build
```

The runner has the `trading-agent-prod` label, which is required by the
workflow. Its registration and work directories are persistent Docker
volumes, and it also uses `restart: unless-stopped`.

After the runner is online, the release flow is:

```bash
git switch prod
git merge main
git push origin prod
```

GitHub Actions checks out that exact commit on `dellg15`, runs
`./deploy/deploy.sh` with the event's `prod` ref and SHA, builds the production
images locally, replaces the containers, and verifies both health endpoints.
Manual and runner deployments share the host-visible `deploy.lock`, so only
one production replacement can run at a time. A failed health check fails the
workflow and prints the recent container logs, so the deployment can be
diagnosed before retrying.

## Persistent state

Production uses `TRADINGAGENTS_HOME=/data` backed by the host directory in
`compose.env`. Development uses a different named Docker volume. This keeps
API-key settings, paper trades, memories, and database history isolated between
the two environments.
