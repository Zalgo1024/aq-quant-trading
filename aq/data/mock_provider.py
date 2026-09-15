"""Mock 数据源：无需联网，生成确定性的合成行情。

用于开发/自测/CI。切换到真实数据只需改 ``data.source``。
"""

from __future__ import annotations

from datetime import date

from aq.core.models import Bar
from aq.data.provider import DataProvider
from aq.execution.feed import MockFeed

# 一小组示例标的（含不同板块，便于验证涨跌停差异）
DEFAULT_UNIVERSE = [
    {"symbol": "600000", "name": "浦发银行", "industry": "银行", "board": "MAIN"},
    {"symbol": "600519", "name": "贵州茅台", "industry": "食品饮料", "board": "MAIN"},
    {"symbol": "000001", "name": "平安银行", "industry": "银行", "board": "MAIN"},
    {"symbol": "000651", "name": "格力电器", "industry": "家用电器", "board": "MAIN"},
    {"symbol": "002415", "name": "海康威视", "industry": "电子", "board": "MAIN"},
    {"symbol": "300750", "name": "宁德时代", "industry": "电力设备", "board": "CHINEXT"},
    {"symbol": "300059", "name": "东方财富", "industry": "非银金融", "board": "CHINEXT"},
    {"symbol": "688981", "name": "中芯国际", "industry": "半导体", "board": "STAR"},
    {"symbol": "601318", "name": "中国平安", "industry": "非银金融", "board": "MAIN"},
    {"symbol": "000858", "name": "五粮液", "industry": "食品饮料", "board": "MAIN"},
    {"symbol": "002594", "name": "比亚迪", "industry": "汽车", "board": "MAIN"},
    {"symbol": "600036", "name": "招商银行", "industry": "银行", "board": "MAIN"},
]


class MockProvider(DataProvider):
    name = "mock"

    def __init__(self, cfg=None) -> None:  # type: ignore[no-untyped-def]
        self.cfg = cfg
        self._feed = MockFeed(seed=42, base_price=20.0, vol=0.022)

    def get_stock_list(self) -> list[dict]:
        out = []
        for i, s in enumerate(DEFAULT_UNIVERSE):
            out.append(
                {
                    "symbol": s["symbol"],
                    "name": s["name"],
                    "industry": s["industry"],
                    "board": s["board"],
                    "list_date": "2015-01-01",
                    "is_st": False,
                }
            )
        return out

    def get_daily(self, symbol, start, end, adjust="post") -> list[Bar]:
        return self._feed.history(symbol, start, end)
