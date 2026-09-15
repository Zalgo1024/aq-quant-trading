"""A 股交易日历。

为什么需要它
------------
回测/模拟里的「下一个交易日」「N 天后调仓」「最近 20 个交易日」这类需求，
如果用自然日推算，遇到周末和春节/国庆就会错位 —— 轻则少算几天，重则
在停市日生成订单、在长假前误判调仓。

本模块从 ``data_cache/calendar.parquet``（由 ``scripts/fetch_meta.py`` 拉取，
源为新浪 ``tool_trade_date_hist_sina``）读取真实交易日历，提供：

- ``is_trading_day(d)``   是否交易日
- ``next_trading_day(d)`` 下一交易日（含当日为交易日时返回当日还是后一日，见参数）
- ``prev_trading_day(d)`` 上一交易日
- ``trading_days(a, b)``  区间内交易日列表
- ``shift(d, n)``         沿交易日历平移 n 个交易日（n 可为负）

文件缺失时不会抛异常，而是退化为「工作日」近似并打印一次警告，
保证 mock 环境 / 尚未拉数据的新机器也能跑通。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from aq.config.settings import PROJECT_ROOT

_CALENDAR_REL = "data_cache/calendar.parquet"


class TradingCalendar:
    """交易日历。惰性加载，全局共享一份即可。"""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            self.path = PROJECT_ROOT / _CALENDAR_REL
        else:
            p = Path(path)
            self.path = p if p.is_absolute() else PROJECT_ROOT / p
        self._days: list[date] | None = None
        self._set: set[date] | None = None
        self._warned = False

    # ---------------------------------------------------------------- 加载
    def load(self) -> list[date]:
        if self._days is not None:
            return self._days
        days: list[date] = []
        if self.path.exists():
            try:
                df = pd.read_parquet(self.path)
                col = "trade_date" if "trade_date" in df.columns else df.columns[0]
                s = pd.to_datetime(df[col]).dt.date
                days = sorted(set(s.tolist()))
            except Exception as exc:  # noqa: BLE001
                if not self._warned:
                    print(f"[警告] 交易日历读取失败（{exc}），退化为工作日近似")
                    self._warned = True
        if not days and not self._warned:
            print(f"[警告] 未找到交易日历 {self.path}，退化为工作日近似。"
                  f"请运行：python scripts/fetch_meta.py --only calendar")
            self._warned = True
        self._days = days
        self._set = set(days)
        return days

    @property
    def available(self) -> bool:
        return bool(self.load())

    # ---------------------------------------------------------------- 查询
    def is_trading_day(self, d) -> bool:
        self.load()
        d = _to_date(d)
        if self._set:
            return d in self._set
        return d.weekday() < 5  # 退化：周一至周五

    def trading_days(self, start=None, end=None) -> list[date]:
        days = self.load()
        if not days:
            return _workdays(start, end)
        if start is not None:
            a = _to_date(start)
            days = [d for d in days if d >= a]
        if end is not None:
            b = _to_date(end)
            days = [d for d in days if d <= b]
        return days

    def next_trading_day(self, d, inclusive: bool = False) -> date:
        """下一交易日。

        ``inclusive=True`` 时若 ``d`` 本身是交易日则直接返回 ``d``。
        """
        self.load()
        d = _to_date(d)
        if self._days:
            for x in self._days:
                if x > d or (inclusive and x == d):
                    return x
            return d  # 超出日历范围
        x = d if (inclusive and d.weekday() < 5) else d + timedelta(days=1)
        while x.weekday() >= 5:
            x += timedelta(days=1)
        return x

    def prev_trading_day(self, d, inclusive: bool = False) -> date:
        self.load()
        d = _to_date(d)
        if self._days:
            for x in reversed(self._days):
                if x < d or (inclusive and x == d):
                    return x
            return d
        x = d if (inclusive and d.weekday() < 5) else d - timedelta(days=1)
        while x.weekday() >= 5:
            x -= timedelta(days=1)
        return x

    def shift(self, d, n: int) -> date:
        """沿交易日历平移 n 个交易日（n 可为负）。"""
        self.load()
        d = _to_date(d)
        if not self._days:
            x, step = d, (1 if n >= 0 else -1)
            for _ in range(abs(n)):
                x += timedelta(days=step)
                while x.weekday() >= 5:
                    x += timedelta(days=step)
            return x

        if n >= 0:
            idx = _bisect_left(self._days, d)
            idx += n
        else:
            idx = _bisect_left(self._days, d) - 1
            idx += n + 1
        idx = max(0, min(idx, len(self._days) - 1))
        return self._days[idx]


# -------------------------------------------------------------------- 单例
_default: TradingCalendar | None = None


def get_calendar(path: str | Path | None = None) -> TradingCalendar:
    """全局共享的日历实例（避免每只股票都重读一次 parquet）。"""
    global _default
    if path is not None:
        return TradingCalendar(path)
    if _default is None:
        _default = TradingCalendar()
    return _default


# -------------------------------------------------------------------- 工具
def _bisect_left(arr: list[date], x: date) -> int:
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _to_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _workdays(start, end) -> list[date]:
    a = _to_date(start) if start is not None else date(2000, 1, 1)
    b = _to_date(end) if end is not None else date.today()
    out, x = [], a
    while x <= b:
        if x.weekday() < 5:
            out.append(x)
        x += timedelta(days=1)
    return out
