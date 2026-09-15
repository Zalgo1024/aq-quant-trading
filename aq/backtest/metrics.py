"""绩效指标计算（对齐 quantstats 的常用指标口径）。

含 A 股常用口径：年化收益（242 交易日）、夏普、索提诺、最大回撤、卡玛、
胜率、盈亏比、换手率、信息比率。
"""

from __future__ import annotations

import math
from statistics import mean, pstdev

from aq.core.models import BacktestMetrics, EquityPoint, Fill, Side
from aq.core.rules import TRADING_DAYS_PER_YEAR


def compute_metrics(
    equity: list[EquityPoint],
    initial_cash: float,
    fills: list[Fill] | None = None,
    risk_free_rate: float = 0.02,
) -> BacktestMetrics:
    if len(equity) < 2:
        return BacktestMetrics()

    values = [p.equity for p in equity]
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]

    total_return = values[-1] / initial_cash - 1
    n = len(rets)
    annual_return = (1 + total_return) ** (TRADING_DAYS_PER_YEAR / max(n, 1)) - 1 if total_return > -1 else -1.0

    sd = pstdev(rets) if len(rets) > 1 else 0.0
    mu = mean(rets) if rets else 0.0
    daily_rf = risk_free_rate / TRADING_DAYS_PER_YEAR

    sharpe = ((mu - daily_rf) / sd * math.sqrt(TRADING_DAYS_PER_YEAR)) if sd else 0.0

    downside = [r for r in rets if r < daily_rf]
    dsd = pstdev(downside) if len(downside) > 1 else 0.0
    sortino = ((mu - daily_rf) / dsd * math.sqrt(TRADING_DAYS_PER_YEAR)) if dsd else 0.0

    max_dd, calmar = _max_drawdown(values)
    if max_dd < 0:
        calmar = annual_return / abs(max_dd)
    else:
        calmar = 0.0

    win_rate, pl_ratio = _trade_stats(fills or [])
    turnover = _turnover(values, fills or [])

    alpha, beta = _alpha_beta(rets)
    ir = _information_ratio(rets)

    return BacktestMetrics(
        total_return=round(total_return, 4),
        annual_return=round(annual_return, 4),
        sharpe=round(sharpe, 3),
        sortino=round(sortino, 3),
        max_drawdown=round(max_dd, 4),
        calmar=round(calmar, 3),
        win_rate=round(win_rate, 4),
        profit_loss_ratio=round(pl_ratio, 3),
        turnover=round(turnover, 4),
        information_ratio=round(ir, 3),
        alpha=round(alpha, 4),
        beta=round(beta, 3),
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _max_drawdown(values: list[float]) -> tuple[float, float]:
    peak = values[0]
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        dd = (v - peak) / peak if peak else 0.0
        max_dd = min(max_dd, dd)
    return max_dd, 0.0


def _trade_stats(fills: list[Fill]) -> tuple[float, float]:
    """用 **FIFO 配对**统计每笔平仓的真实盈亏，再算胜率与盈亏比。

    之前这里的写法是 ``if f.slippage_cost >= 0`` —— 滑点成本恒为非负数，
    导致胜率永远是 100%，指标完全失去意义。

    正确做法：为每只股票维护一个买入批次队列，卖出时按先进先出取出对应
    批次，用「卖出净收入 − 买入总成本（含费用）」判定该笔平仓是盈是亏。

    返回 (胜率, 盈亏比)；无平仓记录时返回 (0.0, 0.0)。
    """
    from collections import defaultdict, deque

    # symbol -> deque([剩余股数, 买入单价, 该批次已摊费用])
    lots: dict[str, deque] = defaultdict(deque)
    pnls: list[float] = []

    for f in fills:
        if not f.filled_qty or f.filled_qty <= 0 or not f.avg_price:
            continue
        if f.side == Side.BUY:
            fees = (f.commission or 0.0) + (f.transfer_fee or 0.0)
            lots[f.symbol].append([float(f.filled_qty), float(f.avg_price), fees])
        elif f.side == Side.SELL:
            q = lots[f.symbol]
            remain = float(f.filled_qty)
            sell_fee_rate = 0.0
            if f.filled_qty:
                sell_fee_rate = ((f.commission or 0.0) + (f.stamp_tax or 0.0)
                                 + (f.transfer_fee or 0.0)) / float(f.filled_qty)
            while remain > 0 and q:
                lot = q[0]
                take = min(remain, lot[0])
                # 该批次买入时已摊的费用，按取用比例分摊
                buy_fee = lot[2] * (take / lot[0]) if lot[0] else 0.0
                pnl = (float(f.avg_price) - lot[1]) * take - buy_fee - sell_fee_rate * take
                pnls.append(pnl)
                lot[0] -= take
                lot[2] -= buy_fee
                remain -= take
                if lot[0] <= 1e-9:
                    q.popleft()

    if not pnls:
        return 0.0, 0.0

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls)

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    pl_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    return win_rate, pl_ratio


def _turnover(values: list[float], fills: list[Fill]) -> float:
    if not values:
        return 0.0
    traded = sum(f.avg_price * f.filled_qty for f in fills)
    avg_equity = mean(values) or 1.0
    years = max(len(values) / TRADING_DAYS_PER_YEAR, 1e-6)
    return traded / avg_equity / years


def _alpha_beta(rets: list[float]) -> tuple[float, float]:
    """简化：以自身为基准时 alpha=0、beta=1；接入基准指数后可替换。"""
    if not rets:
        return 0.0, 0.0
    return 0.0, 1.0


def _information_ratio(rets: list[float]) -> float:
    """简化：无基准时用收益/波动近似。"""
    if len(rets) < 2:
        return 0.0
    sd = pstdev(rets)
    return (mean(rets) / sd * math.sqrt(TRADING_DAYS_PER_YEAR)) if sd else 0.0
