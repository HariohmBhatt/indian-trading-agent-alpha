# Indian Trading Agent — Local Setup Complete

Setup completed on this machine at `/home/hariohm/indian-trading-agent`.

## What is running

| Service | URL | Log |
|---------|-----|-----|
| Backend (FastAPI) | http://localhost:8000 | `/tmp/trading-agent-backend.log` |
| Frontend (Next.js) | http://localhost:3000 | `/tmp/trading-agent-frontend.log` |

Health check: `curl http://localhost:8000/api/health`

## LLM configuration

- **Provider:** OpenAI (saved in SQLite)
- **Deep model:** `gpt-4o`
- **Quick model:** `gpt-4o-mini`

### Add your OpenAI API key (required for Deep Analysis)

**Option A — `.env` file:**

```bash
# Edit /home/hariohm/indian-trading-agent/.env
OPENAI_API_KEY=sk-your-key-here
```

Then restart the backend.

**Option B — UI:**

1. Open http://localhost:3000
2. **Settings → API Keys → OpenAI** → paste key → Test → Save

### Run Deep Analysis after key is set

```bash
cd /home/hariohm/indian-trading-agent
chmod +x scripts/run_deep_analysis.sh
./scripts/run_deep_analysis.sh RELIANCE
```

## Start / stop

### Start (one command)

```bash
cd /home/hariohm/indian-trading-agent
source venv/bin/activate
./start.sh
```

### Start manually (current background mode)

```bash
# Backend
cd /home/hariohm/indian-trading-agent
source venv/bin/activate
uvicorn backend.app:app --host 0.0.0.0 --port 8000

# Frontend (separate terminal)
cd /home/hariohm/indian-trading-agent/frontend
npm run dev -- --port 3000
```

### Stop

```bash
lsof -ti :8000 | xargs -r kill
lsof -ti :3000 | xargs -r kill
```

## Verified free features (API smoke test)

All passed during setup:

- Today dashboard data: daily verdict, regime, FII/DII bias
- Top Picks: `/api/recommend/`
- Market Scan: `/api/scanner/run` (gap on Nifty 50)
- Strategies: support/resistance, pivot points
- News feed, charts, performance stats
- Signal performance, concentration, calendar
- Paper trade created (RELIANCE, id=1)

## Data locations

| Path | Contents |
|------|----------|
| `~/.tradingagents/trading_agent.db` | SQLite: keys, trades, analyses |
| `~/.tradingagents/memory/` | Agent BM25 memories |
| `~/.tradingagents/logs/` | Analysis dumps |
| `~/.tradingagents/cache/` | Market data cache |

## Extension roadmap (when you are ready)

Pick **one** path first after 1–2 weeks of UI exploration.

### Path 1 — Custom screening (no LLM, lowest effort)

| File | What to change |
|------|----------------|
| `backend/scanner.py` | Add scan types; tune gap/volume/breakout thresholds |
| `backend/recommender.py` | Edit `DEFAULT_WEIGHTS`; add signals in `_analyze_stock()` |
| `backend/stock_list.py` | Expand universes (Nifty 500, custom watchlist) |

Use **Signal Performance** page to auto-tune weights from paper trade outcomes.

### Path 2 — Better Indian fundamentals

Replace weak yfinance fundamentals with Screener.in or NSE archives:

| File | What to change |
|------|----------------|
| `tradingagents/dataflows/interface.py` | Register new vendor |
| `tradingagents/default_config.py` | Set `fundamental_data` vendor |
| New module e.g. `tradingagents/dataflows/screener_data.py` | Fetch ROCE, debt, P&L |

### Path 3 — Fyers live data + execution (future)

Not in repo today. Requires:

1. `tradingagents/dataflows/fyers_data.py` — quotes, history, option chain
2. `backend/routers/broker.py` — OAuth token refresh, order placement
3. Keep `order_execution_enabled: False` until paper validation passes
4. Only then enable live orders in `tradingagents/default_config.py`

### Path 4 — TradingView Pine alerts

1. Add `POST /api/webhooks/tradingview` in FastAPI
2. Validate shared secret in payload
3. Map alert → paper trade or recommender boost

### Path 5 — Quality Momentum factor strategy

1. Daily cron: download bhavcopy via `nse-archives`
2. Compute 12-1 momentum + scaled turnover filter
3. Feed top 30 into `backend/recommender.py` as a new signal weight
4. Backtest via `/api/simulation/recommender-backtest`

## Recommended next steps

1. Add `OPENAI_API_KEY` and run `./scripts/run_deep_analysis.sh RELIANCE`
2. Explore UI pages in order: **Today → Top Picks → Market Scan → Simulation**
3. Paper-trade 3–5 picks; let **Signal Performance** accumulate data
4. Choose extension path from table above based on what you use most

## Safety defaults (unchanged)

- `dry_run: True`
- `order_execution_enabled: False`
- No live broker connection
