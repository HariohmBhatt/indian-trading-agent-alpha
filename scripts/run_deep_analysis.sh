#!/usr/bin/env bash
# Run Deep Analysis on a ticker after OPENAI_API_KEY is configured.
# Usage: ./scripts/run_deep_analysis.sh RELIANCE

set -euo pipefail
TICKER="${1:-RELIANCE}"
TRADE_DATE="${2:-$(date +%Y-%m-%d)}"
BASE="${BASE_URL:-http://localhost:8000}"

echo "Checking OpenAI key..."
CONFIGURED=$(curl -s "$BASE/api/settings/api-keys" | python3 -c "import sys,json; print(json.load(sys.stdin).get('openai',{}).get('configured', False))")
if [[ "$CONFIGURED" != "True" ]]; then
  echo "ERROR: OpenAI API key not configured."
  echo "  Option 1: Edit .env → OPENAI_API_KEY=sk-... then restart backend"
  echo "  Option 2: UI → Settings → API Keys → OpenAI → Save"
  exit 1
fi

echo "Starting Deep Analysis for $TICKER on $TRADE_DATE..."
TASK_ID=$(curl -s -X POST "$BASE/api/analysis/run" \
  -H 'Content-Type: application/json' \
  -d "{\"ticker\":\"$TICKER\",\"trade_date\":\"$TRADE_DATE\",\"analysts\":[\"market\",\"social\",\"news\",\"fundamentals\"]}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")

echo "Task ID: $TASK_ID (stream at $BASE/api/analysis/ws/$TASK_ID)"
echo "Polling for result..."

for i in $(seq 1 120); do
  sleep 5
  STATUS=$(curl -s "$BASE/api/analysis/$TASK_ID" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "")
  echo "  [$i] status=$STATUS"
  if [[ "$STATUS" == "completed" ]]; then
    curl -s "$BASE/api/analysis/$TASK_ID" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('Signal:', d.get('signal'))
print('Decision:', (d.get('final_trade_decision') or '')[:500])
"
    exit 0
  fi
  if [[ "$STATUS" == "error" ]]; then
    curl -s "$BASE/api/analysis/$TASK_ID"
    exit 1
  fi
done

echo "TIMEOUT after 10 minutes"
exit 1
