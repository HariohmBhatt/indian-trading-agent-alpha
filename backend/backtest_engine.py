"""Core backtesting engine — runs the AI pipeline on historical dates and tracks P&L."""

import time
import yfinance as yf
from datetime import datetime, timedelta
from typing import Optional
from tradingagents.utils.ticker import normalize_ticker
from tradingagents.utils.market_calendar import is_trading_day, next_trading_day
from backend.execution import (
    MAX_BACKTEST_DATES,
    YFINANCE_TIMEOUT_SECONDS,
    ExecutionTimeout,
    InputLimitExceeded,
    check_deadline,
)


def get_trading_dates(start_date: str, end_date: str, interval_days: int = 5) -> list[str]:
    """Generate a list of trading dates between start and end, spaced by interval_days.

    Args:
        start_date: Start date yyyy-mm-dd
        end_date: End date yyyy-mm-dd
        interval_days: Minimum gap between analysis dates

    Returns:
        List of trading date strings
    """
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()

    dates = []
    current = start
    while current <= end:
        if is_trading_day(current):
            dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=interval_days)
        else:
            current += timedelta(days=1)
    return dates


def get_price_on_date(ticker: str, date_str: str) -> Optional[float]:
    """Get the closing price for a ticker on a specific date."""
    symbol = normalize_ticker(ticker)
    dt = datetime.strptime(date_str, "%Y-%m-%d")

    # Fetch a small window around the date
    start = (dt - timedelta(days=3)).strftime("%Y-%m-%d")
    end = (dt + timedelta(days=1)).strftime("%Y-%m-%d")

    t = yf.Ticker(symbol)
    hist = t.history(
        start=start,
        end=end,
        timeout=YFINANCE_TIMEOUT_SECONDS,
    )

    if hist.empty:
        return None

    # Find closest date
    target = dt.strftime("%Y-%m-%d")
    for idx in hist.index:
        if idx.strftime("%Y-%m-%d") == target:
            return round(float(hist.loc[idx, "Close"]), 2)

    # Fallback: return last available close before target
    hist_before = hist[hist.index <= dt]
    if not hist_before.empty:
        return round(float(hist_before.iloc[-1]["Close"]), 2)
    return None


def get_next_day_price(ticker: str, date_str: str) -> Optional[float]:
    """Get closing price on the next trading day after date."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    next_td = next_trading_day(dt)
    return get_price_on_date(ticker, next_td.strftime("%Y-%m-%d"))


def calculate_trade_pnl(
    signal: str,
    entry_price: float,
    exit_price: float,
    capital: float,
    position_size_pct: float,
) -> dict:
    """Calculate P&L for a single trade.

    Args:
        signal: BUY/OVERWEIGHT/HOLD/SELL/UNDERWEIGHT/SHORT
        entry_price: Price when signal was given
        exit_price: Price on next trading day
        capital: Total portfolio capital
        position_size_pct: % of capital to use per trade

    Returns:
        Dict with pnl_pct, pnl_amount, new_capital
    """
    position_value = capital * (position_size_pct / 100)

    if signal in ("BUY", "OVERWEIGHT", "STRONG BUY"):
        # Long position
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
    elif signal in ("SELL", "UNDERWEIGHT", "SHORT"):
        # Short position
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100
    else:
        # HOLD — no trade
        pnl_pct = 0.0

    pnl_amount = position_value * (pnl_pct / 100)
    new_capital = capital + pnl_amount

    return {
        "pnl_pct": round(pnl_pct, 2),
        "pnl_amount": round(pnl_amount, 2),
        "new_capital": round(new_capital, 2),
    }


def run_backtest(
    ticker: str,
    dates: list[str],
    initial_capital: float,
    position_size_pct: float,
    enable_learning: bool,
    config: dict,
    on_trade_complete=None,
    on_status=None,
    deadline: float | None = None,
):
    """Run the full backtest loop.

    Args:
        ticker: Stock ticker
        dates: List of analysis dates
        initial_capital: Starting capital in INR
        position_size_pct: % of capital per trade
        enable_learning: Whether to call reflect_and_remember after each trade
        config: TradingAgentsGraph config
        on_trade_complete: Callback(trade_dict) after each trade
        on_status: Callback(message) for status updates

    Returns:
        Dict with summary stats and list of trades
    """
    dates = list(dates)
    if len(dates) > MAX_BACKTEST_DATES:
        raise InputLimitExceeded("backtest dates", len(dates), MAX_BACKTEST_DATES)

    from tradingagents.graph.trading_graph import TradingAgentsGraph

    if on_status:
        on_status(f"Initializing AI pipeline for {ticker}...")

    # Single instance — memory accumulates across trades
    ta = TradingAgentsGraph(debug=False, config=config)

    symbol = normalize_ticker(ticker)
    capital = initial_capital
    peak_capital = initial_capital
    max_drawdown = 0.0
    trades = []
    cumulative_pnl = 0.0
    winning = 0
    losing = 0

    for i, date in enumerate(dates):
        check_deadline(deadline, workload="backtest")
        trade_start = time.time()

        if on_status:
            on_status(f"[{i+1}/{len(dates)}] Analyzing {symbol} on {date}...")

        try:
            # Run AI analysis
            final_state, signal = ta.propagate(symbol, date)

            # Get prices
            entry_price = get_price_on_date(ticker, date)
            exit_price = get_next_day_price(ticker, date)

            if entry_price is None or exit_price is None:
                if on_status:
                    on_status(f"  Skipping {date} — price data unavailable")
                continue

            # Calculate P&L
            result = calculate_trade_pnl(signal, entry_price, exit_price, capital, position_size_pct)
            capital = result["new_capital"]
            cumulative_pnl += result["pnl_amount"]

            if result["pnl_pct"] > 0:
                winning += 1
            elif result["pnl_pct"] < 0:
                losing += 1

            # Track drawdown
            if capital > peak_capital:
                peak_capital = capital
            dd = ((peak_capital - capital) / peak_capital) * 100
            if dd > max_drawdown:
                max_drawdown = dd

            duration = round(time.time() - trade_start, 1)

            trade = {
                "trade_date": date,
                "ticker": symbol,
                "signal": signal,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": result["pnl_pct"],
                "pnl_amount": result["pnl_amount"],
                "cumulative_pnl": round(cumulative_pnl, 2),
                "portfolio_value": capital,
                "duration_seconds": duration,
            }
            trades.append(trade)

            if on_trade_complete:
                on_trade_complete(trade)

            if on_status:
                status_emoji = "profit" if result["pnl_pct"] >= 0 else "loss"
                on_status(
                    f"  {signal} @ {entry_price} → {exit_price} | "
                    f"P&L: {result['pnl_pct']:+.2f}% (₹{result['pnl_amount']:+,.0f}) | "
                    f"Portfolio: ₹{capital:,.0f}"
                )

            # Memory learning
            if enable_learning:
                try:
                    ta.reflect_and_remember(result["pnl_amount"])
                    if on_status:
                        on_status(f"  Agent reflected on this trade outcome")
                except Exception as e:
                    if on_status:
                        on_status(f"  Reflection failed: {e}")

        except ExecutionTimeout:
            raise
        except Exception as e:
            if on_status:
                on_status(f"  Error on {date}: {e}")
            continue

    total_return_pct = ((capital - initial_capital) / initial_capital) * 100
    total_trades = winning + losing

    summary = {
        "ticker": symbol,
        "initial_capital": initial_capital,
        "final_portfolio_value": round(capital, 2),
        "position_size_pct": position_size_pct,
        "enable_learning": enable_learning,
        "total_trades": total_trades,
        "winning_trades": winning,
        "losing_trades": losing,
        "win_rate": round(winning / total_trades * 100, 1) if total_trades > 0 else 0,
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "total_pnl": round(cumulative_pnl, 2),
        "status": "completed",
    }

    return summary, trades
