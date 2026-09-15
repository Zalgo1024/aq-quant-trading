"""行情源抽象与实现。

三种模式共用 ``MarketFeed`` 接口：
- ``MockFeed``        —— 随机游走，无需联网，用于开发与自测；
- ``HistoricalFeed``  —— 历史日线，回测用（provider 可插拔）；
- ``ReplayFeed``      —— 把历史数据按时间「回放」，模拟盘/联调用。
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Iterator

from aq.core.models import Bar
from aq.core.rules import limit_prices

BarCallback = Callable[[Bar], None]


class MarketFeed(ABC):
    """行情源抽象。"""

    @abstractmethod
    def history(
        self,
        symbol: str,
        start: str | date,
        end: str | date,
        freq: str = "1d",
    ) -> list[Bar]:
        """返回区间内的 K 线序列（按时间升序）。"""

    @abstractmethod
    def get_bar(self, symbol: str, dt: datetime, freq: str = "1d") -> Bar | None:
        """取指定时点的 K 线；不存在返回 None。"""

    def subscribe(self, symbols: Iterable[str], freq: str = "1d") -> None:
        """订阅实时行情（仅实时源需要实现）。"""
        self._subscribed = list(symbols)  # type: ignore[attr-defined]

    def on_bar(self, cb: BarCallback) -> None:
        """注册逐 bar 回调（仅实时/回放源会触发）。"""
        self._on_bar_cb = cb  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Mock：随机游走
# --------------------------------------------------------------------------


class MockFeed(MarketFeed):
    """合成行情：几何随机游走 + 涨跌停边界，便于离线开发与单测。

    同一 symbol 的序列是确定性的（由种子决定），保证测试可复现。
    """

    def __init__(self, seed: int = 42, base_price: float = 20.0, vol: float = 0.02) -> None:
        self.seed = seed
        self.base_price = base_price
        self.vol = vol
        self._cache: dict[str, dict[str, Bar]] = {}
        self._on_bar_cb: BarCallback | None = None

    # -- 内部：确保某 symbol 的序列已生成 ----------------------------------
    def _ensure_series(self, symbol: str, start: date, end: date) -> None:
        if symbol in self._cache:
            return
        rng = random.Random(f"{self.seed}:{symbol}")
        series: dict[str, Bar] = {}

        # 从 start 往前多生成 60 天，便于计算前置指标
        cur = start - timedelta(days=90)
        price = self.base_price
        while cur <= end:
            if cur.weekday() < 5:  # 跳过周末（简化处理，未接入真实交易日历）
                drift = 0.0002
                ret = rng.gauss(drift, self.vol)
                pre_close = price
                close = max(1.0, pre_close * (1 + ret))
                high = max(pre_close, close) * (1 + abs(rng.gauss(0, self.vol / 2)))
                low = min(pre_close, close) * (1 - abs(rng.gauss(0, self.vol / 2)))
                up, down = limit_prices(pre_close, symbol)
                # 贴边处理：超出涨跌停则截断
                close = min(max(close, down), up)
                high = min(max(high, close), up)
                low = max(min(low, close), down)

                vol_ = rng.randint(1_000_000, 30_000_000)
                series[cur.strftime("%Y-%m-%d")] = Bar(
                    symbol=symbol,
                    time=datetime.combine(cur, datetime.min.time()),
                    freq="1d",
                    open=round(pre_close, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    close=round(close, 2),
                    volume=float(vol_),
                    amount=round(vol_ * close, 2),
                    pre_close=round(pre_close, 2),
                    limit_up=up,
                    limit_down=down,
                    is_trading=True,
                )
                price = close
            cur += timedelta(days=1)

        self._cache[symbol] = series

    # -- MarketFeed 实现 ---------------------------------------------------
    def history(
        self,
        symbol: str,
        start: str | date,
        end: str | date,
        freq: str = "1d",
    ) -> list[Bar]:
        s = _as_date(start)
        e = _as_date(end)
        self._ensure_series(symbol, s, e)
        out = [b for k, b in sorted(self._cache[symbol].items()) if s <= _as_date(k) <= e]
        return out

    def get_bar(self, symbol: str, dt: datetime, freq: str = "1d") -> Bar | None:
        d = dt.date()
        self._ensure_series(symbol, d, d)
        return self._cache[symbol].get(d.strftime("%Y-%m-%d"))


# --------------------------------------------------------------------------
# Historical：基于数据层 provider（P0 接入 akshare/tushare）
# --------------------------------------------------------------------------


class HistoricalFeed(MarketFeed):
    """包装数据层 provider 的历史行情。

    provider 需实现 ``get_daily(symbol, start, end) -> list[Bar]``。
    """

    def __init__(self, provider: object) -> None:
        self.provider = provider
        self._idx: dict[str, dict[str, Bar]] = {}

    def _load(self, symbol: str, start: date, end: date) -> dict[str, Bar]:
        key = f"{symbol}"
        if key not in self._idx:
            bars = self.provider.get_daily(symbol, start, end)  # type: ignore[attr-defined]
            self._idx[key] = {b.time.strftime("%Y-%m-%d"): b for b in bars}
        return self._idx[key]

    def history(self, symbol, start, end, freq="1d") -> list[Bar]:
        s, e = _as_date(start), _as_date(end)
        series = self._load(symbol, s, e)
        return [b for k, b in sorted(series.items()) if s <= _as_date(k) <= e]

    def get_bar(self, symbol, dt, freq="1d") -> Bar | None:
        d = dt.date()
        return self._load(symbol, d, d).get(d.strftime("%Y-%m-%d"))


# --------------------------------------------------------------------------
# Replay：按时间回放（模拟盘/联调）
# --------------------------------------------------------------------------


class ReplayFeed(MarketFeed):
    """把历史行情当作"实时"逐日回放，用于模拟盘演练。"""

    def __init__(self, inner: MarketFeed, symbols: list[str], start: str | date, end: str | date) -> None:
        self.inner = inner
        self.symbols = symbols
        self.start = _as_date(start)
        self.end = _as_date(end)
        self._on_bar_cb: BarCallback | None = None

    def history(self, symbol, start, end, freq="1d") -> list[Bar]:
        return self.inner.history(symbol, start, end, freq)

    def get_bar(self, symbol, dt, freq="1d") -> Bar | None:
        return self.inner.get_bar(symbol, dt, freq)

    def __iter__(self) -> Iterator[tuple[datetime, dict[str, Bar]]]:
        """按交易日逐个吐出全市场截面。"""
        # 用第一个 symbol 的交易日作为日历基准
        cal = sorted({b.time.date() for b in self.inner.history(self.symbols[0], self.start, self.end)})
        for d in cal:
            dt = datetime.combine(d, datetime.min.time())
            bar_map: dict[str, Bar] = {}
            for sym in self.symbols:
                b = self.get_bar(sym, dt)
                if b is not None:
                    bar_map[sym] = b
            if self._on_bar_cb:
                for b in bar_map.values():
                    self._on_bar_cb(b)
            yield dt, bar_map


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _as_date(v: str | date | datetime) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(v[:10], "%Y-%m-%d").date()
