"""因子库。

第一阶段实现纯量价因子（无需财务数据即可运行），后续可扩展财务/资金流/情绪因子。

每个因子函数签名统一为::

    def factor_xxx(bars: list[Bar]) -> float | None

返回该股票在**最后一根 bar 时点**的因子值；数据不足返回 None。
"""

from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Callable

from aq.core.models import Bar

FactorFn = Callable[[list[Bar]], float | None]


# --------------------------------------------------------------------------
# 动量 / 反转
# --------------------------------------------------------------------------


def momentum(bars: list[Bar], window: int = 20) -> float | None:
    """N 日动量：(close_t / close_{t-N}) - 1。"""
    if len(bars) < window + 1:
        return None
    return bars[-1].close / bars[-window - 1].close - 1


def reversal(bars: list[Bar], window: int = 5) -> float | None:
    """短期反转：取负的 N 日收益（A 股短期反转效应显著）。"""
    m = momentum(bars, window)
    return None if m is None else -m


def volatility(bars: list[Bar], window: int = 20) -> float | None:
    """年化波动率（越低越好时取负）。"""
    if len(bars) < window + 1:
        return None
    rets = [bars[i].close / bars[i - 1].close - 1 for i in range(1, len(bars))][-window:]
    return pstdev(rets) * math.sqrt(242)


# --------------------------------------------------------------------------
# 均线 / 趋势
# --------------------------------------------------------------------------


def ma_bias(bars: list[Bar], window: int = 20) -> float | None:
    """均线乖离率：(close - MA_N) / MA_N。"""
    if len(bars) < window:
        return None
    ma = mean(b.close for b in bars[-window:])
    return (bars[-1].close - ma) / ma if ma else None


def ma_cross(bars: list[Bar], short: int = 5, long: int = 20) -> float | None:
    """均线交叉信号：短均线相对长均线的偏离。"""
    if len(bars) < long:
        return None
    s = mean(b.close for b in bars[-short:])
    l = mean(b.close for b in bars[-long:])
    return (s - l) / l if l else None


# --------------------------------------------------------------------------
# 量能
# --------------------------------------------------------------------------


def volume_ratio(bars: list[Bar], window: int = 20) -> float | None:
    """量比：当日成交量 / N 日均量。"""
    if len(bars) < window + 1:
        return None
    avg = mean(b.volume for b in bars[-window - 1 : -1])
    return bars[-1].volume / avg if avg else None


def turnover_change(bars: list[Bar], window: int = 5, base: int = 20) -> float | None:
    """换手率突变：近 window 日均量 / 近 base 日均量。"""
    if len(bars) < base + window:
        return None
    recent = mean(b.volume for b in bars[-window:])
    old = mean(b.volume for b in bars[-base - window : -window])
    return recent / old if old else None


# --------------------------------------------------------------------------
# 距上次信号（复用彩票项目"遗漏"思路）
# --------------------------------------------------------------------------


def days_since_high(bars: list[Bar], window: int = 60) -> float | None:
    """距上次 N 日新高的天数（归一化）。"""
    if len(bars) < window:
        return None
    seg = bars[-window:]
    hi = max(b.high for b in seg)
    for i in range(len(seg) - 1, -1, -1):
        if seg[i].high >= hi - 1e-6:
            return (len(seg) - 1 - i) / window
    return 1.0


def limit_up_count(bars: list[Bar], window: int = 20) -> float:
    """近 N 日涨停次数（需 bars 携带 limit_up）。"""
    seg = bars[-window:]
    return float(sum(1 for b in seg if b.limit_up and b.close >= b.limit_up - 1e-6))


def gap(bars: list[Bar]) -> float | None:
    """跳空缺口：(open_t - close_{t-1}) / close_{t-1}。"""
    if len(bars) < 2:
        return None
    pc = bars[-2].close
    return (bars[-1].open - pc) / pc if pc else None


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------


class FactorLibrary:
    """因子注册表：名称 -> 计算函数。

    使用统一签名 ``fn(bars) -> float | None``，便于批量计算与中性化。
    """

    def __init__(self) -> None:
        self._factors: dict[str, FactorFn] = {}
        self._directions: dict[str, int] = {}  # +1 越大越好，-1 越小越好
        self._register_defaults()

    def register(self, name: str, fn: FactorFn, direction: int = 1) -> None:
        self._factors[name] = fn
        self._directions[name] = direction

    def _register_defaults(self) -> None:
        reg = self.register
        reg("momentum_20", lambda b: momentum(b, 20))
        reg("reversal_5", lambda b: reversal(b, 5))
        reg("volatility_20", lambda b: volatility(b, 20), direction=-1)
        reg("ma_bias_20", lambda b: ma_bias(b, 20))
        reg("ma_cross_5_20", lambda b: ma_cross(b, 5, 20))
        reg("volume_ratio", lambda b: volume_ratio(b, 20))
        reg("turnover_change", lambda b: turnover_change(b, 5, 20))
        reg("days_since_high", lambda b: days_since_high(b, 60), direction=-1)
        reg("limit_up_count", lambda b: limit_up_count(b, 20))
        reg("gap", lambda b: gap(b))

    # ------------------------------------------------------------------ API
    @property
    def names(self) -> list[str]:
        return list(self._factors)

    def direction(self, name: str) -> int:
        return self._directions.get(name, 1)

    def compute(self, symbol: str, bars: list[Bar]) -> dict[str, float | None]:
        """计算单只股票的全部因子值。"""
        out: dict[str, float | None] = {}
        for name, fn in self._factors.items():
            try:
                out[name] = fn(bars)
            except (ValueError, ZeroDivisionError, IndexError):
                out[name] = None
        return out


def compute_factors(symbol: str, bars: list[Bar], library: FactorLibrary | None = None) -> dict[str, float | None]:
    lib = library or FactorLibrary()
    return lib.compute(symbol, bars)
