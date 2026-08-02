"""SQLite database for watchlist, analysis history, backtests, and settings."""

import sqlite3
import os
import json
from contextlib import contextmanager

from backend.database_safety import reject_unexpected_empty_database
from backend.migrations import upgrade_database

TRADINGAGENTS_HOME = os.getenv(
    "TRADINGAGENTS_HOME",
    os.path.join(os.path.expanduser("~"), ".tradingagents"),
)
DB_PATH = os.getenv(
    "TRADINGAGENTS_DB_PATH",
    os.path.join(TRADINGAGENTS_HOME, "trading_agent.db"),
)


def ensure_db():
    """Create or upgrade the database through the migration runner."""

    return upgrade_database(DB_PATH)


@contextmanager
def get_db():
    path = reject_unexpected_empty_database(DB_PATH)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Watchlist ---

def get_watchlist() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]


def add_to_watchlist(ticker: str, exchange: str = "NSE", name: str = None):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (ticker, exchange, name) VALUES (?, ?, ?)",
            (ticker.upper(), exchange, name),
        )


def remove_from_watchlist(ticker: str):
    with get_db() as conn:
        conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker.upper(),))


# --- Analysis History ---

def save_analysis(task_id: str, data: dict):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO analysis_history
            (task_id, ticker, trade_date, signal, market_report, sentiment_report,
             news_report, fundamentals_report, investment_plan, trader_investment_plan,
             final_trade_decision, bull_history, bear_history,
             risk_aggressive_history, risk_conservative_history, risk_neutral_history,
             stats, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                data.get("ticker"),
                data.get("trade_date"),
                data.get("signal"),
                data.get("market_report"),
                data.get("sentiment_report"),
                data.get("news_report"),
                data.get("fundamentals_report"),
                data.get("investment_plan"),
                data.get("trader_investment_plan"),
                data.get("final_trade_decision"),
                data.get("bull_history"),
                data.get("bear_history"),
                data.get("risk_aggressive_history"),
                data.get("risk_conservative_history"),
                data.get("risk_neutral_history"),
                json.dumps(data.get("stats")) if data.get("stats") else None,
                data.get("duration_seconds"),
            ),
        )


def update_analysis_pnl(task_id: str, entry_price: float, exit_price: float, pnl_amount: float, pnl_pct: float, pnl_status: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE analysis_history SET entry_price=?, exit_price=?, pnl_amount=?, pnl_pct=?, pnl_status=? WHERE task_id=?",
            (entry_price, exit_price, pnl_amount, pnl_pct, pnl_status, task_id),
        )


def get_analysis(task_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM analysis_history WHERE task_id = ?", (task_id,)).fetchone()
        if row:
            d = dict(row)
            if d.get("stats"):
                d["stats"] = json.loads(d["stats"])
            return d
        return None


def get_analysis_history(limit: int = 50, offset: int = 0) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT task_id, ticker, trade_date, signal, duration_seconds,
                      entry_price, exit_price, pnl_pct, pnl_status, created_at
               FROM analysis_history ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Backtest ---

def save_backtest_run(backtest_id: str, data: dict):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO backtest_runs
            (backtest_id, ticker, initial_capital, position_size_pct, enable_learning,
             total_trades, winning_trades, losing_trades, total_return_pct,
             max_drawdown_pct, final_portfolio_value, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                backtest_id,
                data.get("ticker"),
                data.get("initial_capital"),
                data.get("position_size_pct"),
                data.get("enable_learning"),
                data.get("total_trades", 0),
                data.get("winning_trades", 0),
                data.get("losing_trades", 0),
                data.get("total_return_pct", 0),
                data.get("max_drawdown_pct", 0),
                data.get("final_portfolio_value"),
                data.get("status", "running"),
            ),
        )


def save_backtest_trade(backtest_id: str, trade: dict):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO backtest_trades
            (backtest_id, trade_date, ticker, signal, entry_price, exit_price,
             pnl_amount, pnl_pct, cumulative_pnl, portfolio_value, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                backtest_id,
                trade.get("trade_date"),
                trade.get("ticker"),
                trade.get("signal"),
                trade.get("entry_price"),
                trade.get("exit_price"),
                trade.get("pnl_amount"),
                trade.get("pnl_pct"),
                trade.get("cumulative_pnl"),
                trade.get("portfolio_value"),
                trade.get("duration_seconds"),
            ),
        )


def get_backtest_run(backtest_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM backtest_runs WHERE backtest_id = ?", (backtest_id,)).fetchone()
        return dict(row) if row else None


def get_backtest_trades(backtest_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM backtest_trades WHERE backtest_id = ? ORDER BY trade_date",
            (backtest_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_backtest_history(limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Settings ---

def get_setting(key: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str | None):
    with get_db() as conn:
        if value is None or value == "":
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


def get_all_settings() -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# --- Equity Portfolio Reviews ---

def save_equity_portfolio_review(review: dict):
    """Persist a Kite equity portfolio review."""
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO equity_portfolio_reviews
            (review_id, review_date, holdings_json, summary_json, insights_json, model_metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                review["review_id"],
                review["review_date"],
                json.dumps(review.get("holdings", [])),
                json.dumps(review.get("summary", {})),
                json.dumps(review.get("insights", {})),
                json.dumps(review.get("model_metadata", {})),
            ),
        )


def _decode_equity_portfolio_review(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["holdings"] = json.loads(data.pop("holdings_json") or "[]")
    data["summary"] = json.loads(data.pop("summary_json") or "{}")
    data["insights"] = json.loads(data.pop("insights_json") or "{}")
    data["model_metadata"] = json.loads(data.pop("model_metadata_json") or "{}")
    return data


def get_latest_equity_portfolio_review() -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM equity_portfolio_reviews
            ORDER BY created_at DESC, review_id DESC LIMIT 1"""
        ).fetchone()
        return _decode_equity_portfolio_review(row)


def get_equity_portfolio_review(review_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM equity_portfolio_reviews WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        return _decode_equity_portfolio_review(row)


def list_equity_portfolio_reviews(limit: int = 30) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM equity_portfolio_reviews
            ORDER BY created_at DESC, review_id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [_decode_equity_portfolio_review(r) for r in rows]


# --- Paper Trades ---

def add_paper_trade(data: dict) -> int:
    """Open a new paper trade. Returns the new row ID."""
    # Keep the historical private helper as a compatibility seam.  It now
    # delegates to the same versioned runner used during application startup.
    _migrate_paper_trades_columns()

    triggered = data.get("triggered_signals")
    if triggered is not None and not isinstance(triggered, str):
        triggered = json.dumps(triggered)

    # Tag the trade with today's market regime so we can later compute
    # conditional signal performance (some signals only work in BULL, etc.)
    regime_at_entry = data.get("regime_at_entry")
    if regime_at_entry is None:
        try:
            from backend.market_regime import get_current_regime
            regime_at_entry = get_current_regime().get("regime")
        except Exception:
            regime_at_entry = None

    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO paper_trades
            (ticker, source, strategy, direction, signal, score, confidence,
             success_probability, triggered_signals, entry_price, notes, regime_at_entry)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("ticker"),
                data.get("source", "manual"),
                data.get("strategy"),
                data.get("direction", "LONG"),
                data.get("signal"),
                data.get("score"),
                data.get("confidence"),
                data.get("success_probability"),
                triggered,
                data.get("entry_price"),
                data.get("notes"),
                regime_at_entry,
            ),
        )
        return cursor.lastrowid


def _migrate_paper_trades_columns():
    """Compatibility wrapper for callers of the former lazy migration."""

    return ensure_db()


def list_paper_trades(status: str | None = None) -> list[dict]:
    _migrate_paper_trades_columns()
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE status = ? ORDER BY entry_datetime DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM paper_trades ORDER BY entry_datetime DESC"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("triggered_signals"):
                try:
                    d["triggered_signals"] = json.loads(d["triggered_signals"])
                except Exception:
                    pass
            result.append(d)
        return result


def update_paper_trade_prices(trade_id: int, prices: dict):
    """Update tracked prices + P&L percentages for a paper trade."""
    with get_db() as conn:
        # Get current trade to calculate P&L
        row = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        if not row:
            return
        entry = row["entry_price"]
        direction = row["direction"]
        multiplier = 1 if direction == "LONG" else -1

        def calc_pnl(exit_price):
            if not exit_price or not entry:
                return None
            return round(multiplier * (exit_price - entry) / entry * 100, 2)

        conn.execute(
            """UPDATE paper_trades SET
                price_1d = COALESCE(?, price_1d),
                price_3d = COALESCE(?, price_3d),
                price_5d = COALESCE(?, price_5d),
                price_10d = COALESCE(?, price_10d),
                pnl_1d_pct = COALESCE(?, pnl_1d_pct),
                pnl_3d_pct = COALESCE(?, pnl_3d_pct),
                pnl_5d_pct = COALESCE(?, pnl_5d_pct),
                pnl_10d_pct = COALESCE(?, pnl_10d_pct),
                updated_at = datetime('now')
               WHERE id = ?""",
            (
                prices.get("price_1d"),
                prices.get("price_3d"),
                prices.get("price_5d"),
                prices.get("price_10d"),
                calc_pnl(prices.get("price_1d")),
                calc_pnl(prices.get("price_3d")),
                calc_pnl(prices.get("price_5d")),
                calc_pnl(prices.get("price_10d")),
                trade_id,
            ),
        )


def update_paper_trade_status(trade_id: int, status: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE paper_trades SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, trade_id),
        )


def delete_paper_trade(trade_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM paper_trades WHERE id = ?", (trade_id,))


# --- Recommender Backtest ---

def save_recommender_backtest_row(data: dict):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO recommender_backtests
            (run_id, trade_date, ticker, signal, score, confidence, success_probability,
             entry_price, return_1d, return_3d, return_5d, return_10d, outcome_1d, outcome_5d)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("run_id"),
                data.get("trade_date"),
                data.get("ticker"),
                data.get("signal"),
                data.get("score"),
                data.get("confidence"),
                data.get("success_probability"),
                data.get("entry_price"),
                data.get("return_1d"),
                data.get("return_3d"),
                data.get("return_5d"),
                data.get("return_10d"),
                data.get("outcome_1d"),
                data.get("outcome_5d"),
            ),
        )


def get_recommender_backtest(run_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM recommender_backtests WHERE run_id = ? ORDER BY trade_date, ticker",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_recommender_backtest_runs() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT run_id, trade_date,
                      COUNT(*) as signals,
                      SUM(CASE WHEN outcome_5d='win' THEN 1 ELSE 0 END) as wins,
                      SUM(CASE WHEN outcome_5d='loss' THEN 1 ELSE 0 END) as losses,
                      AVG(return_5d) as avg_return_5d,
                      MAX(created_at) as created_at
               FROM recommender_backtests
               GROUP BY run_id
               ORDER BY MAX(created_at) DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


# --- Positions (local store, synced from Kite on demand) ---

POSITION_FIELDS = (
    "tradingsymbol", "exchange", "isin", "product", "quantity", "t1_quantity",
    "average_price", "last_price", "close_price", "invested_value", "current_value",
    "pnl", "pnl_pct", "day_change", "day_change_pct", "source", "notes",
)


def list_positions(source: str | None = None) -> list[dict]:
    with get_db() as conn:
        if source:
            rows = conn.execute(
                "SELECT * FROM positions WHERE source = ? ORDER BY current_value DESC",
                (source,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM positions ORDER BY current_value DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_position(tradingsymbol: str, exchange: str = "NSE") -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE tradingsymbol = ? AND exchange = ?",
            (tradingsymbol.upper(), exchange.upper()),
        ).fetchone()
        return dict(row) if row else None


def upsert_position(data: dict):
    """Insert or update a position. Derived values are computed by the caller."""
    symbol = (data.get("tradingsymbol") or "").upper()
    exchange = (data.get("exchange") or "NSE").upper()
    if not symbol:
        raise ValueError("tradingsymbol is required")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO positions
            (tradingsymbol, exchange, isin, product, quantity, t1_quantity,
             average_price, last_price, close_price, invested_value, current_value,
             pnl, pnl_pct, day_change, day_change_pct, source, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tradingsymbol, exchange) DO UPDATE SET
                isin = COALESCE(excluded.isin, isin),
                product = COALESCE(excluded.product, product),
                quantity = excluded.quantity,
                t1_quantity = excluded.t1_quantity,
                average_price = excluded.average_price,
                last_price = excluded.last_price,
                close_price = excluded.close_price,
                invested_value = excluded.invested_value,
                current_value = excluded.current_value,
                pnl = excluded.pnl,
                pnl_pct = excluded.pnl_pct,
                day_change = excluded.day_change,
                day_change_pct = excluded.day_change_pct,
                source = excluded.source,
                notes = COALESCE(excluded.notes, notes),
                updated_at = datetime('now')""",
            (
                symbol,
                exchange,
                data.get("isin"),
                data.get("product"),
                data.get("quantity", 0),
                data.get("t1_quantity", 0),
                data.get("average_price", 0),
                data.get("last_price"),
                data.get("close_price"),
                data.get("invested_value"),
                data.get("current_value"),
                data.get("pnl"),
                data.get("pnl_pct"),
                data.get("day_change"),
                data.get("day_change_pct"),
                data.get("source", "manual"),
                data.get("notes"),
            ),
        )


def replace_kite_positions(rows: list[dict]) -> dict:
    """Sync Kite holdings into the local store.

    Upserts every Kite row and deletes kite-sourced rows that are no longer
    present in Kite (i.e. fully exited positions). Manual rows are untouched.
    Returns counts for the sync summary.
    """
    ensure_db()
    symbols = {(r.get("tradingsymbol") or "").upper() for r in rows}
    with get_db() as conn:
        existing = {
            r["tradingsymbol"].upper()
            for r in conn.execute("SELECT tradingsymbol FROM positions WHERE source = 'kite'").fetchall()
        }
    added = len(symbols - existing)
    updated = len(symbols & existing)
    removed = len(existing - symbols)

    for row in rows:
        upsert_position({**row, "source": "kite"})

    if removed:
        with get_db() as conn:
            if symbols:
                placeholders = ",".join("?" * len(symbols))
                conn.execute(
                    f"DELETE FROM positions WHERE source = 'kite' AND tradingsymbol NOT IN ({placeholders})",
                    tuple(sorted(symbols)),
                )
            else:
                conn.execute("DELETE FROM positions WHERE source = 'kite'")
    return {"added": added, "updated": updated, "removed": removed, "total": len(rows)}


def delete_position(tradingsymbol: str, exchange: str = "NSE") -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM positions WHERE tradingsymbol = ? AND exchange = ?",
            (tradingsymbol.upper(), exchange.upper()),
        )
        return cursor.rowcount > 0
