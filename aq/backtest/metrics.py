"""绩效指标计算（对齐 quantstats 的常用指标口径）。

含 A 股常用口径：年化收益（242 交易日）、夏普、索提诺、最大回撤、卡玛、
胜率、盈亏比、换手率、信息比率、alpha/beta。

基准口径
--------
``alpha`` / ``beta`` / ``information_ratio`` 三个指标**必须有基准序列**
才有意义。基准来自 ``data_cache/index_bars/``（见 ``aq.data.index_store``），
由回测引擎读入后以 ``benchmark_rets`` 传入。

**不传基准时不再伪造指标**：早期实现里 ``_alpha_beta`` 直接
``return 0.0, 1.0``、``_information_ratio`` 用「自身收益/波动」冒充，
与真实信息比率毫无关系（后者衡量的是**超额**收益的稳定性），却在报告
里当作正常指标展示。现在的行为：

- 传了基准 → 用真实 OLS 回归求 alpha/beta，用超额收益算信息比率；
- 未传基准 → 这三个指标返回 ``None``（在 ``BacktestMetrics`` 里显式
  标注为「未接基准」），调用方可据此决定是否展示。
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
    benchmark_rets: list[float] | None = None,
) -> BacktestMetrics:
    """算绩效指标。

    ``benchmark_rets`` 为**与策略日收益逐日对齐**的基准日收益序列
    （长度须与策略日收益一致）。为 ``None`` 时 alpha/beta/信息比率
    返回 ``None``，不伪造。
    """
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

    # 基准相关指标：无基准则返回 None（不伪造）
    bench = _align_benchmark(rets, benchmark_rets)
    if bench is None:
        alpha = beta = ir = None
    else:
        alpha, beta = _alpha_beta(rets, bench)
        ir = _information_ratio(rets, bench)

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
        information_ratio=None if ir is None else round(ir, 3),
        alpha=None if alpha is None else round(alpha, 4),
        beta=None if beta is None else round(beta, 3),
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _align_benchmark(
    rets: list[float], benchmark_rets: list[float] | None
) -> list[float] | None:
    """把基准收益对齐到策略收益长度；不匹配时返回 ``None`` 并提示。

    宁可返回 ``None``（指标不展示）也不要拿错位的序列算出一个看起来
    正常的假 alpha。
    """
    if benchmark_rets is None:
        return None
    b = [float(x) for x in benchmark_rets]
    if len(b) == len(rets):
        return b
    if not b or not rets:
        return None
    print(
        f"[警告] 基准序列长度({len(b)})与策略收益长度({len(rets)})不一致，"
        f"alpha/beta/信息比率置为未计算"
    )
    return None


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


def _alpha_beta(rets: list[float], benchmark_rets: list[float]) -> tuple[float, float]:
    """对基准做 OLS 回归：``r_p = alpha + beta * r_b + eps``。

    - ``beta = Cov(r_p, r_b) / Var(r_b)``
    - ``alpha_daily = mean(r_p) - beta * mean(r_b)``，再年化：
      ``alpha = alpha_daily * TRADING_DAYS_PER_YEAR``

    年化口径说明：``alpha`` 是**年化超额**（算术年化，非复利），这是
    业界对 alpha 的常见口径（对应「年化 alpha」报表值）。若基准方差为 0
    （基准恒定）则退回 ``beta=0, alpha=年化策略收益``。
    """
    n = len(rets)
    if n < 2 or len(benchmark_rets) != n:
        return 0.0, 0.0

    mu_p, mu_b = mean(rets), mean(benchmark_rets)
    cov = sum((rets[i] - mu_p) * (benchmark_rets[i] - mu_b) for i in range(n)) / (n - 1)
    var_b = sum((benchmark_rets[i] - mu_b) ** 2 for i in range(n)) / (n - 1)

    if var_b <= 0:
        return mu_p * TRADING_DAYS_PER_YEAR, 0.0

    beta = cov / var_b
    alpha_daily = mu_p - beta * mu_b
    return alpha_daily * TRADING_DAYS_PER_YEAR, beta


def _information_ratio(rets: list[float], benchmark_rets: list[float]) -> float:
    """信息比率 = 年化**超额**收益 / 跟踪误差。

    ``excess = r_p - r_b``；``IR = mean(excess) / std(excess) * sqrt(252)``。

    与夏普的区别：夏普除的是**绝对**波动，IR 除的是**相对基准**的跟踪
    误差。早期实现用 ``mean(r_p)/std(r_p)`` 冒充 IR，那其实是夏普，
    会系统性高估（漏掉了与基准共动的部分）。

    跟踪误差为 0（策略与基准完全同步）时 IR 无定义，返回 0.0。
    """
    n = len(rets)
    if n < 2 or len(benchmark_rets) != n:
        return 0.0

    excess = [rets[i] - benchmark_rets[i] for i in range(n)]
    te = pstdev(excess)
    if te <= 0:
        return 0.0
    return mean(excess) / te * math.sqrt(TRADING_DAYS_PER_YEAR)
