#!/usr/bin/env bash
# Install and enable trading-agent systemd services (run once with sudo).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_DIR="/etc/systemd/system"

echo "Building frontend (API URL for Tailscale: http://dellg15:8000)..."
cd "$ROOT/frontend"
NEXT_PUBLIC_API_URL=http://dellg15:8000 npm run build

echo "Installing systemd units..."
install -m 644 "$ROOT/deploy/systemd/trading-agent-backend.service" "$SYSTEMD_DIR/"
install -m 644 "$ROOT/deploy/systemd/trading-agent-frontend.service" "$SYSTEMD_DIR/"

systemctl daemon-reload
systemctl enable trading-agent-backend.service trading-agent-frontend.service
systemctl restart trading-agent-backend.service trading-agent-frontend.service

echo ""
echo "Status:"
systemctl --no-pager status trading-agent-backend.service --lines=3 || true
systemctl --no-pager status trading-agent-frontend.service --lines=3 || true
echo ""
echo "Health: curl http://localhost:8000/api/health"
echo "UI (Tailscale): http://dellg15:3000"
