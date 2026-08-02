# Portfolio Optimization Agent System Research

Date: 2026-07-10

## Scope

This note investigates how to design an agent system that continuously improves the existing Kite-backed equity portfolio review, adds clearer portfolio insights, and surfaces technical signals for Indian stocks while reusing the repo's current feedback loops and Telegram notification path. Sources are limited to this repository plus official Kite Connect and Telegram Bot API documentation.

## Primary Sources

- Kite Connect portfolio docs say the portfolio API includes long-term equity holdings and up-to-date profit/loss computations, and the holdings endpoint is `GET /portfolio/holdings`: [Kite Connect portfolio docs](https://kite.trade/docs/connect/v3/portfolio/) lines 54-68.
- Kite Connect Python docs describe the web-app flow as client initialization, redirect to `login_url()`, request-token capture, `generate_session()`, storing the access token, and using that token for later calls: [pykiteconnect docs](https://kite.trade/docs/pykiteconnect/v4/) lines 126-135.
- Kite Connect Python docs expose `holdings()` as the equity-holdings method and `set_session_expiry_hook()` for token errors: [pykiteconnect docs](https://kite.trade/docs/pykiteconnect/v4/) lines 1520-1530 and 2109-2118.
- Telegram Bot API `sendMessage` accepts `chat_id`, `text` of 1-4096 characters after entity parsing, optional `parse_mode`, and optional JSON-serialized `reply_markup`: [Telegram Bot API docs](https://core.telegram.org/bots/api) lines 2621-2636.
- Telegram Bot API supports HTML formatting in `parse_mode=HTML` and requires raw `<`, `>`, and `&` characters outside tags/entities to be escaped: [Telegram Bot API docs](https://core.telegram.org/bots/api) lines 2637-2641 and 2705-2737.
- The repo's declared backend dependencies include `langgraph`, `rank-bm25`, `yfinance`, and `kiteconnect`: `pyproject.toml:11-34`.

## Current Repo Facts

- The Kite adapter stores API key, API secret, access token, token date, and profile in the `settings` table, and it considers the user connected only when the stored access-token date equals today's date: `backend/brokers/kite.py:10-64`.
- The Kite adapter implements the same login pattern as the official Python docs: `get_login_url()` calls `login_url()`, `exchange_request_token()` calls `generate_session()`, stores the returned `access_token`, stores the token date, and calls `set_access_token()`: `backend/brokers/kite.py:99-131`.
- `fetch_equity_holdings()` obtains an authenticated Kite client, calls `client.holdings()`, clears the token on detected auth errors, and returns normalized holding rows: `backend/brokers/kite.py:134-166`.
- Holding normalization preserves `tradingsymbol`, `exchange`, `isin`, `product`, `quantity`, `t1_quantity`, `average_price`, `last_price`, `close_price`, invested/current value, total P&L, total P&L %, day change, and day change %: `backend/brokers/kite.py:176-207`.
- The equity portfolio review currently calculates total invested value, total current value, total P&L, day P&L, per-holding allocation, sector allocation, top winners, top losers, high-risk holdings, concentration warnings, and a plain summary: `backend/equity_portfolio.py:90-207`.
- The current review enriches up to the largest 25 holdings by current value with `_analyze_stock()`, then chooses actions using P&L, allocation, quantity, recommendation direction, and recommendation score: `backend/equity_portfolio.py:29-87` and `backend/equity_portfolio.py:103-128`.
- Portfolio reviews are persisted into `equity_portfolio_reviews`, whose columns store review id/date plus holdings, summary, insights, and metadata as JSON text: `backend/db.py:190-200` and `backend/db.py:401-455`.
- The equity portfolio router can fetch holdings, create a review, retrieve latest/history/detail reviews, send an existing review to Telegram, or run a review and send it to Telegram; when Kite auth is missing, the run-and-send route sends a Kite login reminder instead: `backend/routers/equity_portfolio.py:42-120`.
- The Telegram helper stores bot token, chat id, enabled flag, and app URL in settings; `send_message()` posts to `/sendMessage`, truncates text to 4096 characters, optionally sends `parse_mode`, and JSON-serializes `reply_markup`: `backend/notifications/telegram.py:13-17` and `backend/notifications/telegram.py:91-117`.
- Telegram portfolio messages already use HTML escaping, include summary value/P&L/day P&L, high-risk flags, concentration warnings, and a link to the portfolio page: `backend/notifications/telegram.py:150-192`.
- The recommender has default signal weights for gap, volume, breakout, support/resistance, cyclical, RSI, and trend signals, and it refreshes active weights from settings before scoring: `backend/recommender.py:17-78` and `backend/recommender.py:447-449`.
- `_analyze_stock()` currently fetches six months of Yahoo Finance history for `<ticker>.NS`, computes gap, volume spike, breakout/breakdown, support/resistance proximity, RSI, cyclical month, and moving-average trend signals, then returns price, score, direction, confidence, success probability, signal list, support, and resistance: `backend/recommender.py:93-271`.
- `recommend()` scans a universe in a `ThreadPoolExecutor`, applies market-bias, event, and concentration adjustments, returns ranked buy/sell buckets, exposes active-regime metadata, and records qualifying recommendations as shadow trades: `backend/recommender.py:429-552`.
- Paper trades store source, strategy, direction, signal, score, confidence, success probability, triggered signals, entry price, horizon prices/P&L, status, notes, and regime-at-entry; `add_paper_trade()` serializes triggered signals and auto-tags the current regime: `backend/db.py:87-113` and `backend/db.py:460-500`.
- `refresh_paper_trade_prices()` updates elapsed 1/3/5/10 trading-day prices for active paper trades, expires old active trades, and then best-effort refreshes shadow-trade prices: `backend/simulation.py:163-214`.
- Shadow trades record STRONG BUY and selected BUY recommendations idempotently by `(ticker, signal_date)`, include the original triggered signals and regime-at-entry, and later backfill 1/3/5/10 day P&L: `backend/shadow_trades.py:1-19`, `backend/shadow_trades.py:35-117`, and `backend/shadow_trades.py:119-170`.
- Signal performance explodes `triggered_signals` from closed paper trades, attributes each closed trade to each signal type present, computes win rate, Wilson lower bound, average 5-day return, and suggested weights, and persists applied tuned weights under `recommender_tuned_weights`: `backend/signal_performance.py:1-19`, `backend/signal_performance.py:245-382`, and `backend/signal_performance.py:431-458`.
- Regime-conditional performance splits signal outcomes by `BULL`, `BEAR`, `SIDEWAYS`, and `HIGH_VOL`, and applied regime weights are stored under `recommender_regime_weights`: `backend/signal_performance.py:95-242` and `backend/signal_performance.py:489-626`.
- The market-regime classifier uses Nifty `^NSEI`, 50/200 SMA alignment, and a 20-day realized-volatility check against a sampled 120-day baseline, returning `BULL`, `BEAR`, `SIDEWAYS`, `HIGH_VOL`, or `UNKNOWN`: `backend/market_regime.py:1-15` and `backend/market_regime.py:45-134`.
- Confidence calibration reads closed paper trades with `success_probability`, computes a Brier score, reliability bins, predicted/actual averages, calibration gap, and over/under/well-calibrated verdict: `backend/confidence_calibration.py:1-24` and `backend/confidence_calibration.py:50-215`.
- The multi-agent analysis path already runs long work in daemon threads, streams graph progress over WebSockets, saves completed analysis rows, and uses `TradingAgentsGraph` for the LangGraph workflow: `backend/routers/analysis.py:21-43`, `backend/routers/analysis.py:53-178`, and `backend/routers/analysis.py:181-211`.
- Backtests use the same repo pattern of daemon thread plus WebSocket status events and DB persistence: `backend/routers/backtest.py:34-123`.
- The FastAPI lifespan currently initializes the DB and loads settings, and the app includes the Kite, equity portfolio, Telegram, recommender, signal-performance, regime, confidence-calibration, simulation, shadow-trades, and memory routers: `backend/app.py:20-27` and `backend/app.py:55-79`.
- The deployed backend is already modeled as a long-running systemd service that starts `uvicorn backend.app:app --host 0.0.0.0 --port 8000`: `deploy/systemd/trading-agent-backend.service:1-20`.
- The TradingAgents graph initializes BM25-backed memories for bull, bear, trader, investment judge, and portfolio manager roles, creates analyst tool nodes, compiles a LangGraph workflow, logs final states, and can reflect on returns/losses into memory: `tradingagents/graph/trading_graph.py:95-132`, `tradingagents/graph/trading_graph.py:192-225`, and `tradingagents/graph/trading_graph.py:267-283`.
- The graph's control flow loops analysts back to tools while tool calls exist, alternates bull/bear debate until the configured debate limit, and alternates risk debaters until the configured risk-discussion limit: `tradingagents/graph/conditional_logic.py:14-67` and `tradingagents/graph/setup.py:139-198`.
- The portfolio-manager agent prompt already asks for rating, entry, stop-loss, targets, position size, horizon, risk-reward, summary, thesis, and Indian-market risk factors, and it retrieves prior portfolio-manager memories before deciding: `tradingagents/agents/managers/portfolio_manager.py:18-71`.
- Memory entries are stored on disk with `created_at`, `last_accessed`, `hit_count`, BM25 retrieval, age decay, recent-access bonus, stats, and pruning helpers: `tradingagents/agents/utils/memory.py:1-9`, `tradingagents/agents/utils/memory.py:80-119`, `tradingagents/agents/utils/memory.py:186-265`, and `tradingagents/agents/utils/memory.py:283-388`.

## Design Implications

1. Add a portfolio-orchestrator layer, not a separate optimizer. The repo already has data acquisition, technical scoring, persistence, outcome measurement, confidence calibration, regime weighting, and Telegram delivery, so the new agent should compose `fetch_equity_holdings()`, `build_equity_portfolio_review()`, `_analyze_stock()` or a public wrapper around it, `refresh_paper_trade_prices()`, `compute_signal_performance()`, `compute_regime_conditional_weights()`, `compute_calibration()`, and `build_portfolio_review_message()` rather than duplicate those paths: `backend/brokers/kite.py:157-166`, `backend/equity_portfolio.py:90-207`, `backend/recommender.py:93-271`, `backend/simulation.py:163-214`, `backend/signal_performance.py:245-382`, `backend/signal_performance.py:489-626`, `backend/confidence_calibration.py:50-215`, and `backend/notifications/telegram.py:150-192`.

2. Keep Kite as the source of truth for holdings and use yfinance-backed local scoring as the first technical layer. Kite's official holdings endpoint owns current delivery holdings and P&L fields, while the repo already normalizes those holdings and already computes local Indian-stock technical signals from `<ticker>.NS` history: [Kite Connect portfolio docs](https://kite.trade/docs/connect/v3/portfolio/) lines 54-68, `backend/brokers/kite.py:176-207`, and `backend/recommender.py:93-271`.

3. Make insights explicit and auditable. The current review has raw holdings, summary, action counts, high-risk holdings, and concentration warnings, but a clearer agent payload should add stable categories such as `portfolio_health`, `urgent_actions`, `technical_conflicts`, `trend_support`, `profit_protection`, `drawdown_watch`, `concentration_risk`, `signal_quality`, and `next_review_reason`, each derived from existing holdings, recommendation signals, calibration stats, and regime metadata: `backend/equity_portfolio.py:160-205`, `backend/recommender.py:256-271`, `backend/confidence_calibration.py:203-215`, and `backend/market_regime.py:125-134`.

4. Treat auto-optimization as reviewable until enough outcome data exists. The repo has endpoints/functions that can persist tuned and regime-specific weights, and the recommender picks those weights up on the next run, so a continuous agent can compute suggestions daily but should separate "computed suggestion" from "applied change" unless policy says to auto-apply after minimum sample thresholds: `backend/signal_performance.py:52-61`, `backend/signal_performance.py:341-358`, `backend/signal_performance.py:431-458`, `backend/signal_performance.py:480-486`, `backend/signal_performance.py:586-626`, and `backend/recommender.py:45-78`.

5. Use shadow trades as the optimizer's counterfactual feed. Recommender calls already record qualifying recommendations even when the user does not track them, and shadow refresh backfills outcome horizons, so portfolio optimization should report both user-tracked and skipped-pick evidence before changing weights or user guidance: `backend/recommender.py:543-550`, `backend/shadow_trades.py:35-117`, `backend/shadow_trades.py:119-170`, and `backend/shadow_trades.py:199-279`.

6. Prefer a scheduled job boundary over running optimization inside every portfolio request. The repo has long-running daemon-thread patterns for explicit analysis/backtest requests and a systemd backend service, but the portfolio review route is synchronous and directly fetches holdings plus enrichment, so continuous optimization should be triggered by a dedicated scheduled route/job such as `run_portfolio_cycle()` rather than hidden inside `GET /holdings` or `POST /reviews`: `backend/routers/analysis.py:199-204`, `backend/routers/backtest.py:118-123`, `deploy/systemd/trading-agent-backend.service:1-20`, and `backend/routers/equity_portfolio.py:42-120`.

7. Preserve the current Telegram transport and make the digest fit Telegram's constraints. The current helper already sends HTML messages, escapes user-visible content in the portfolio formatter, truncates to 4096 characters, and supports inline URL buttons; any richer digest should prioritize concise action sections and link to the full portfolio page for details: `backend/notifications/telegram.py:91-129`, `backend/notifications/telegram.py:150-192`, and [Telegram Bot API docs](https://core.telegram.org/bots/api) lines 2621-2639.

8. Use the TradingAgents memory/reflection loop for slower qualitative lessons, not for the first-pass daily portfolio optimizer. The graph already has a portfolio-manager memory and reflection path, but the portfolio review module is fast and local, so the first continuous version should run deterministic enrichment and outcome metrics daily, then optionally feed summarized failed/successful portfolio decisions into memory after enough P&L evidence exists: `tradingagents/graph/trading_graph.py:95-124`, `tradingagents/graph/trading_graph.py:267-283`, `tradingagents/agents/managers/portfolio_manager.py:18-71`, and `tradingagents/agents/utils/memory.py:186-265`.

## Proposed Agent System

### 1. Portfolio Cycle Orchestrator

The orchestrator should run one idempotent cycle for a target date: check Kite status, fetch holdings, build/enrich the review, refresh paper/shadow outcomes, compute optimizer diagnostics, persist one review/result payload, and send a Telegram summary or Kite-login reminder. This design reuses the existing Kite status/token checks, holdings fetch, review creation, outcome refresh, signal-performance functions, calibration function, review persistence, and Telegram reminder path: `backend/brokers/kite.py:47-64`, `backend/brokers/kite.py:157-166`, `backend/equity_portfolio.py:231-234`, `backend/simulation.py:163-214`, `backend/signal_performance.py:245-382`, `backend/confidence_calibration.py:50-215`, `backend/db.py:401-455`, and `backend/routers/equity_portfolio.py:99-120`.

### 2. Technical Signal Enricher

The enrichment agent should expose the existing `_analyze_stock()` result in a portfolio-friendly shape: current price, day change, score, direction, confidence, success probability, RSI, nearest support, nearest resistance, top bullish/bearish signals, active regime, and the number of active regime overrides. The raw fields already exist in `_analyze_stock()` and `recommend()` returns active-regime metadata, so the main implementation work is a public wrapper and a stable response schema: `backend/recommender.py:256-271` and `backend/recommender.py:528-540`.

### 3. Insight Synthesizer

The insight synthesizer should convert numeric review and signal data into explainable buckets: "keep", "review", "trim", "exit review", "technical conflict", "large winner protect", "drawdown with weak signal", "oversized position", "sector concentration", "missing signal", and "confidence/calibration warning". These buckets map directly to existing action constants, action reasons, concentration warnings, per-holding recommendations, and calibration outputs: `backend/equity_portfolio.py:10-14`, `backend/equity_portfolio.py:47-87`, `backend/equity_portfolio.py:148-181`, and `backend/confidence_calibration.py:191-215`.

### 4. Optimization Advisor

The optimization advisor should compute but not necessarily apply four daily diagnostics: refreshed trade outcomes, global signal-weight suggestions, regime-specific weight suggestions, and confidence-calibration verdict. The functions already exist and are individually callable, and the existing apply functions make persistence explicit enough for an approval workflow: `backend/simulation.py:163-214`, `backend/signal_performance.py:245-382`, `backend/signal_performance.py:489-626`, `backend/confidence_calibration.py:50-215`, and `backend/routers/signal_performance.py:43-86`.

### 5. Notification Agent

The notification agent should send only the decision-critical slice to Telegram: status, portfolio P&L, day P&L, urgent action count, top 3 risk flags, top 3 technical conflicts, optimizer verdict, and a portfolio-page link. This stays inside Telegram's message-length and HTML-formatting constraints while using the existing message/button helper: [Telegram Bot API docs](https://core.telegram.org/bots/api) lines 2621-2639, [Telegram Bot API docs](https://core.telegram.org/bots/api) lines 2705-2737, `backend/notifications/telegram.py:91-129`, and `backend/notifications/telegram.py:150-192`.

### 6. Continuous Execution

For a first production shape, prefer an explicit scheduled endpoint/job that calls the orchestrator once per market day after close and can be invoked by a systemd timer or a small admin action, because the repo already runs the backend under systemd and already uses explicit background threads for long manual jobs. If later in-process scheduling is added, it should be guarded by a DB-backed idempotency key such as `(cycle_date, cycle_type)` so multiple backend reloads or duplicate triggers do not send duplicate Telegram messages or apply weights twice: `deploy/systemd/trading-agent-backend.service:1-20`, `backend/routers/analysis.py:199-204`, `backend/routers/backtest.py:118-123`, `backend/db.py:82-85`, and `backend/db.py:190-200`.

## Suggested Payload Shape

```json
{
  "cycle_date": "YYYY-MM-DD",
  "kite": {"connected_today": true},
  "review": {"review_id": "...", "status": "STABLE|REVIEW_NEEDED|EMPTY"},
  "portfolio_health": {
    "plain_summary": "...",
    "total_current": 0,
    "total_pnl_pct": 0,
    "day_pnl_pct": 0,
    "risk_flag_count": 0,
    "concentration_flag_count": 0
  },
  "holdings": [
    {
      "tradingsymbol": "RELIANCE",
      "allocation_pct": 0,
      "pnl_pct": 0,
      "action": "HOLD|WATCH|REVIEW|TRIM_CONSIDER|EXIT_REVIEW",
      "technical": {
        "direction": "BUY|SELL|STRONG BUY|STRONG SELL|NEUTRAL",
        "score": 0,
        "confidence": "LOW|MEDIUM|HIGH",
        "success_probability": 0,
        "rsi": 0,
        "near_support": 0,
        "near_resistance": 0,
        "signals": []
      },
      "insight_tags": ["technical_conflict", "large_winner_protect"]
    }
  ],
  "optimizer": {
    "signal_performance": {"computed": true, "applied": false},
    "regime_weights": {"computed": true, "applied": false},
    "calibration": {"verdict": "well_calibrated|overconfident|underconfident|no_data"},
    "shadow_comparison": {"filter_verdict": "filter_helps|filter_hurts|filter_neutral|insufficient_data"}
  },
  "telegram": {"sent": true, "message_id": 0}
}
```

Every field above maps to existing repo sources: Kite status exists in `backend/brokers/kite.py:47-64`; review status and summary exist in `backend/equity_portfolio.py:160-205`; holding actions exist in `backend/equity_portfolio.py:10-14` and `backend/equity_portfolio.py:47-87`; technical fields exist in `backend/recommender.py:256-271`; optimizer diagnostics exist in `backend/signal_performance.py:245-382`, `backend/signal_performance.py:489-626`, `backend/confidence_calibration.py:50-215`, and `backend/shadow_trades.py:199-279`; Telegram send metadata is returned by existing send routes in `backend/routers/equity_portfolio.py:72-86`.

## Implementation Notes For Later

- Make `_analyze_stock()` public or wrap it before reusing it broadly, because the portfolio module currently imports a private function directly: `backend/equity_portfolio.py:29-44` and `backend/recommender.py:93-271`.
- Add idempotent cycle persistence before enabling scheduled sends, because current portfolio reviews use random 12-character review IDs and `INSERT OR REPLACE` by review ID rather than one row per date/cycle type: `backend/equity_portfolio.py:183-205` and `backend/db.py:401-417`.
- Keep apply-vs-suggest separate in the API contract, because `apply_tuned_weights()` and `apply_regime_weights()` persist settings immediately and `recommend()` refreshes those settings at the start of each run: `backend/signal_performance.py:431-458`, `backend/signal_performance.py:586-606`, and `backend/recommender.py:447-449`.
- Escape all generated Telegram insight text before sending HTML, because Telegram's HTML mode only supports specific tags and requires raw `<`, `>`, and `&` outside tags/entities to be escaped: [Telegram Bot API docs](https://core.telegram.org/bots/api) lines 2705-2737 and `backend/notifications/telegram.py:150-192`.
- Keep the first continuous cycle read-only with respect to Kite orders, because the current Kite code only implements login/status/holdings and the repo's default configuration includes dry-run and order-execution flags: `backend/brokers/kite.py:1-207` and `backend/app.py:90-96`.

