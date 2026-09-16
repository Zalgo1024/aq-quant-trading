"""本地 parquet 数据源（P1 起回测/研究的默认数据源）。

为什么必须有它
--------------
``AkshareProvider`` 每次 ``get_daily`` 都要联网，而一次全市场回测要读
5000+ 只股票 × 2000+ 个交易日 —— 走网络既慢又会被限流，还无法离线复现实验。

``scripts/fetch_daily.py`` 把数据落盘到 ``data_cache/bars/{symbol}.parquet`` 后，
回测与因子研究一律走本模块读本地文件，网络只负责「增量更新」。

⚠️ 本模块**不提供指数行情**
--------------------------
``data_cache/bars/`` 里只有**个股**。指数代码与深市个股代码撞车
（``000001`` 上证指数/平安银行、``000905`` 中证500/厦门港务 …），
若在此目录按代码取「指数」会静默拿到个股数据。指数行情在
``data_cache/index_bars/``，见 ``aq.data.index_store``。

用法::

    # config/base.yaml
    data:
      source: local
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from aq.config.settings import PROJECT_ROOT
from aq.core.models import Bar


class LocalProvider:
    name = "local"

    def __init__(self, cfg=None, root: str | Path | None = None) -> None:  # type: ignore[no-untyped-def]
        self.cfg = cfg
        if root:
            self.root = Path(root)
        else:
            cache = getattr(getattr(cfg, "data", None), "cache_dir", "data_cache")
            self.root = Path(cache) / "bars" if not Path(cache).is_absolute() else Path(cache) / "bars"
            if not self.root.is_absolute():
                self.root = PROJECT_ROOT / self.root
        self.root.mkdir(parents=True, exist_ok=True)
        self._list_cache: list[dict] | None = None

    # ------------------------------------------------------------------ 列表
    def get_stock_list(self) -> list[dict]:
        if self._list_cache is not None:
            return self._list_cache

        meta = self.root.parent / "stock_list.parquet"
        if meta.exists():
            try:
                df = pd.read_parquet(meta)
                self._list_cache = df.to_dict(orient="records")
                return self._list_cache
            except Exception:  # noqa: BLE001
                pass

        # 退化：扫描文件名（拿不到中文名，只有代码）
        out = []
        for p in sorted(self.root.glob("*.parquet")):
            code = p.stem
            out.append(
                {
                    "symbol": code,
                    "name": code,
                    "industry": "",
                    "board": _board_of(code),
                    "list_date": "",
                    "is_st": False,
                }
            )
        self._list_cache = out
        return out

    def symbols(self) -> list[str]:
        """实际已落盘的股票代码。

        注意与 ``get_stock_list()`` 的区别：后者返回的是**全市场元数据**
        （5562 只，来自 stock_list.parquet，即便某些股票还没拉过数据）；
        本方法只返回 data_cache/bars 下真正存在 parquet 的，避免回测时
        对几千只空股票做无效读取。

        ⚠️ **本目录下没有指数**（重要）
        ----------------------------------
        ``data_cache/bars/`` 里的 ``000001 / 000016 / 000852 / 000905``
        这四个文件是**个股**，不是同名指数：

        ===========  ================  ==========================
        代码          指数语义           bars/ 里实际存的
        ===========  ================  ==========================
        ``000001``    上证指数           平安银行
        ``000016``    上证50              *ST康佳A
        ``000852``    中证1000           石化机械
        ``000905``    中证500            厦门港务
        ===========  ================  ==========================

        A 股指数代码空间与深市个股代码空间撞车，因此**不要**从本目录取
        基准数据（把 ``bars["000905"]`` 当基准 → 拿到厦门港务的股价）。
        指数行情在 ``data_cache/index_bars/``，用
        ``aq.data.index_store.IndexBarStore`` 读写；沪深300 是
        ``000300.SH`` —— 本目录下**根本不存在** ``000300``。
        """
        return sorted(p.stem for p in self.root.glob("*.parquet"))

    def universe(self) -> list[str]:
        """全市场代码（含尚未拉取数据的）。"""
        return [s["symbol"] for s in self.get_stock_list()]

    def has(self, symbol: str) -> bool:
        return (self.root / f"{symbol}.parquet").exists()

    # ------------------------------------------------------------------ 日线
    def get_daily(self, symbol: str, start, end, adjust="post") -> list[Bar]:
        p = self.root / f"{symbol}.parquet"
        if not p.exists():
            return []
        df = pd.read_parquet(p)
        if df.empty:
            return []

        df["date"] = pd.to_datetime(df["time"]).dt.date
        d0 = _to_date(_fmt(start))
        d1 = _to_date(_fmt(end))
        df = df.loc[(df["date"] >= d0) & (df["date"] <= d1)]
        if df.empty:
            return []

        # 去掉 model_dump 时多余的内部列，只保留 Bar 字段
        keep = [c for c in Bar.model_fields if c in df.columns]
        df = df[keep]
        return [Bar(**row) for row in df.to_dict(orient="records")]

    def get_daily_df(self, symbol: str, start=None, end=None) -> pd.DataFrame:
        """直接返回 DataFrame（因子计算/批量分析时比 list[Bar] 快得多）。"""
        p = self.root / f"{symbol}.parquet"
        if not p.exists():
            return pd.DataFrame()
        df = pd.read_parquet(p)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["time"]).dt.date
        if start is not None:
            df = df[df["date"] >= _to_date(_fmt(start))]
        if end is not None:
            df = df[df["date"] <= _to_date(_fmt(end))]
        return df.reset_index(drop=True)

    # ------------------------------------------------------------------ 面板
    def get_panel(self, field: str, symbols: list[str] | None = None,
                  start=None, end=None) -> pd.DataFrame:
        """拼出 date × symbol 的面板（因子研究常用）。空数据自动跳过。

        ``field`` 支持派生字段（parquet 里没有的列，按需计算）：
        - ``close_raw`` / ``open_raw`` / ``high_raw`` / ``low_raw``：真实价 = 复权价 / adj_factor
        - ``adj_factor``：复权因子
        - 其余按原列名取（close / open / volume / amount ...）
        """
        syms = symbols or self.symbols()
        series = {}
        for s in syms:
            df = self.get_daily_df(s, start, end)
            if df.empty:
                continue
            col = _resolve_field(df, field)
            if col is None:
                continue
            series[s] = pd.Series(col.values, index=df["date"].values)
        if not series:
            return pd.DataFrame()
        return pd.DataFrame(series).sort_index()


def _resolve_field(df: pd.DataFrame, field: str):  # type: ignore[no-untyped-def]
    """把字段名解析成一列数据，支持 *_raw 这类派生字段。"""
    if field in df.columns:
        return df[field]

    if field.endswith("_raw"):
        base = field[: -len("_raw")]
        if base in df.columns:
            af = df["adj_factor"] if "adj_factor" in df.columns else 1.0
            af = af.replace(0, 1.0).fillna(1.0)
            return df[base] / af

    if field == "adj_factor" and "adj_factor" in df.columns:
        return df["adj_factor"].replace(0, 1.0).fillna(1.0)

    return None


def _board_of(code: str) -> str:
    if code.startswith("688"):
        return "STAR"
    if code.startswith(("300", "301")):
        return "CHINEXT"
    if code.startswith(("8", "4")):
        return "BSE"
    return "MAIN"


def _to_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _fmt(v) -> str:  # type: ignore[no-untyped-def]
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]
