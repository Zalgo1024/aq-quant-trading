"""tushare 数据源实现（可选，需积分）。

与 akshare 版保持同一 schema，切换只改配置 ``data.source: tushare``。
需要设置环境变量 ``TUSHARE_TOKEN``。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from aq.core.models import Bar
from aq.core.rules import limit_prices
from aq.data.provider import DataProvider


class TushareProvider(DataProvider):
    name = "tushare"

    def __init__(self, cfg=None) -> None:  # type: ignore[no-untyped-def]
        import tushare as ts

        token = os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            raise ImportError("未设置 TUSHARE_TOKEN 环境变量")
        ts.set_token(token)
        self.pro = ts.pro_api()
        self.cfg = cfg
        cache_dir = getattr(getattr(cfg, "data", None), "cache_dir", "data_cache")
        self.cache_dir = Path(cache_dir)
        if not self.cache_dir.is_absolute():
            from aq.config.settings import PROJECT_ROOT

            self.cache_dir = PROJECT_ROOT / self.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_stock_list(self) -> list[dict]:
        df = self.pro.stock_basic(exchange="", list_status="L")
        out = []
        for _, r in df.iterrows():
            out.append(
                {
                    "symbol": str(r["symbol"]).zfill(6),
                    "name": r["name"],
                    "industry": r.get("industry", ""),
                    "board": _board_of(str(r["symbol"])),
                    "list_date": str(r.get("list_date", "")),
                    "is_st": "ST" in str(r["name"]).upper(),
                }
            )
        return out

    def get_daily(self, symbol: str, start, end, adjust="post") -> list[Bar]:
        ts_code = _to_ts_code(symbol)
        s, e = _fmt(start).replace("-", ""), _fmt(end).replace("-", "")
        df = self.pro.daily(ts_code=ts_code, start_date=s, end_date=e)
        if df is None or df.empty:
            return []
        df = df.sort_values("trade_date")
        bars: list[Bar] = []
        pre_close = None
        for _, r in df.iterrows():
            d = datetime.strptime(str(r["trade_date"]), "%Y%m%d").date()
            close = float(r["close"])
            up = down = None
            if pre_close:
                up, down = limit_prices(pre_close, symbol)
            bars.append(
                Bar(
                    symbol=symbol,
                    time=datetime.combine(d, datetime.min.time()),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=close,
                    volume=float(r["vol"]) * 100,   # tushare vol 单位为手
                    amount=float(r["amount"]) * 1000,
                    pre_close=pre_close,
                    limit_up=up,
                    limit_down=down,
                    is_trading=float(r["vol"]) > 0,
                )
            )
            pre_close = close
        return bars


def _board_of(code: str) -> str:
    if code.startswith("688"):
        return "STAR"
    if code.startswith(("300", "301")):
        return "CHINEXT"
    if code.startswith(("8", "4")):
        return "BSE"
    return "MAIN"


def _to_ts_code(symbol: str) -> str:
    if symbol.endswith((".SH", ".SZ", ".BJ")):
        return symbol
    if symbol.startswith(("6", "9")):
        return f"{symbol}.SH"
    if symbol.startswith(("4", "8")):
        return f"{symbol}.BJ"
    return f"{symbol}.SZ"


def _fmt(v) -> str:  # type: ignore[no-untyped-def]
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]
