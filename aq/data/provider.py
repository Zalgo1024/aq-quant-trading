"""数据 Provider 抽象。

设计目标（参考 zvt 的思路）：**让 provider 适配统一 schema，而不是让系统迁就 provider**。
换数据源（akshare / tushare / 本地 parquet）时，上层代码零改动。

P0 阶段只需实现 ``get_daily`` 与 ``get_stock_list``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from aq.core.models import Bar


class DataProvider(ABC):
    """行情数据源接口。"""

    name: str = "abstract"

    @abstractmethod
    def get_stock_list(self) -> list[dict]:
        """返回股票列表：[{symbol, name, list_date, industry, board, is_st}, ...]。"""

    @abstractmethod
    def get_daily(
        self,
        symbol: str,
        start: str | date,
        end: str | date,
        adjust: str = "post",
    ) -> list[Bar]:
        """返回后复权日线。**必须包含 limit_up / limit_down / is_trading**。"""

    def get_trade_calendar(self, start: str | date, end: str | date) -> list[date]:
        """交易日历。默认用某只股票的日线日期近似。"""
        bars = self.get_daily("000001", start, end)
        return [b.time.date() for b in bars]


def get_provider(cfg) -> DataProvider:  # type: ignore[no-untyped-def]
    """按配置创建 provider；依赖缺失时自动降级为 Mock。"""
    source = (cfg.data.source or "mock").lower()

    # P1 起：全市场数据已落盘到 data_cache/bars，回测/研究一律读本地
    if source in ("local", "parquet", "file"):
        from aq.data.local_provider import LocalProvider

        p = LocalProvider(cfg)
        if p.symbols():
            return p  # type: ignore[return-value]
        print("[data] data_cache/bars 为空，自动降级为 mock provider")
        print("       请先执行：python scripts/fetch_daily.py")
        from aq.data.mock_provider import MockProvider

        return MockProvider(cfg)

    if source == "akshare":
        try:
            from aq.data.akshare_provider import AkshareProvider

            return AkshareProvider(cfg)
        except ImportError:
            print("[data] 未安装 akshare，自动降级为 mock provider")
            from aq.data.mock_provider import MockProvider

            return MockProvider(cfg)

    if source == "tushare":
        try:
            from aq.data.tushare_provider import TushareProvider

            return TushareProvider(cfg)
        except ImportError:
            print("[data] 未安装 tushare，自动降级为 mock provider")
            from aq.data.mock_provider import MockProvider

            return MockProvider(cfg)

    from aq.data.mock_provider import MockProvider

    return MockProvider(cfg)
