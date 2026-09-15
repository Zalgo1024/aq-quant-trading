"""股票池（Universe）选择。

为什么需要它
------------
回测引擎原先直接取股票列表的前 ``top_k * 3`` 只 —— 那等于「按代码排序的前 60 只」，
既不是沪深 300，也没有排除 ST / 次新股，选出来的组合没有任何代表性，
回测结果也就无从判断策略好坏。

本模块把 ``config/*.yaml`` 里的 ``universe`` 配置真正落地：

- **指数池**：从 ``data_cache/index_constituents.parquet`` 取沪深300 / 中证500 /
  上证50 / 中证1000 / 全市场；
- **排除 ST / 退市**：名称含 ST、*ST、退；
- **次新股过滤**：上市不足 ``min_list_days`` 个自然日不参与（需要上市日期，
  由 ``scripts/fetch_meta.py`` 从交易所列表补齐）；
- **流动性过滤**：最近 N 个交易日日均成交额低于 ``min_turnover`` 的剔除
  （需要本地行情，可选，``--no-liquidity`` 关闭）；
- **规模上限**：``max_symbols``，防止全市场 5000+ 只把内存吃光。

用法::

    from aq.data.universe import select_universe
    syms = select_universe(cfg)                    # 按配置
    syms = select_universe(cfg, index="zz1000")    # 覆盖指数池
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from aq.config.settings import PROJECT_ROOT

# 配置里的池名 → 指数代码
INDEX_ALIAS = {
    "hs300": "000300", "csi300": "000300", "000300": "000300", "沪深300": "000300",
    "zz500": "000905", "csi500": "000905", "000905": "000905", "中证500": "000905",
    "sz50": "000016", "000016": "000016", "上证50": "000016",
    "zz1000": "000852", "csi1000": "000852", "000852": "000852", "中证1000": "000852",
    "all": None, "market": None, "全市场": None, "": None,
}


class UniverseSelector:
    def __init__(self, cfg=None, cache_dir: str | Path | None = None) -> None:  # type: ignore[no-untyped-def]
        self.cfg = cfg
        d = cache_dir or getattr(getattr(cfg, "data", None), "cache_dir", "data_cache")
        p = Path(d)
        self.cache = p if p.is_absolute() else PROJECT_ROOT / p
        self._meta: pd.DataFrame | None = None
        self._cons: pd.DataFrame | None = None

    # ---------------------------------------------------------------- 数据
    @property
    def meta(self) -> pd.DataFrame:
        if self._meta is None:
            p = self.cache / "stock_list.parquet"
            self._meta = pd.read_parquet(p) if p.exists() else pd.DataFrame(
                columns=["symbol", "name", "industry", "board", "list_date", "is_st"])
        return self._meta

    @property
    def constituents(self) -> pd.DataFrame:
        if self._cons is None:
            p = self.cache / "index_constituents.parquet"
            self._cons = pd.read_parquet(p) if p.exists() else pd.DataFrame(
                columns=["index_code", "symbol"])
        return self._cons

    # ---------------------------------------------------------------- 主入口
    def select(
        self,
        index: str | None = None,
        asof: str | date | None = None,
        exclude_st: bool = True,
        min_list_days: int | None = None,
        min_turnover: float | None = None,
        lookback: int = 20,
        max_symbols: int | None = None,
        require_data: bool = True,
    ) -> list[str]:
        cfg_u = getattr(self.cfg, "universe", None)
        index = index if index is not None else (cfg_u.index if cfg_u else "hs300")
        min_list_days = (min_list_days if min_list_days is not None
                         else (cfg_u.min_list_days if cfg_u else 60))

        df = self.meta.copy()
        if df.empty:
            return []

        # 1) 指数池
        code = INDEX_ALIAS.get(str(index).lower(), None)
        if code is not None:
            cons = self.constituents
            if not cons.empty:
                pool = set(cons.loc[cons["index_code"] == code, "symbol"].astype(str))
                if pool:
                    df = df[df["symbol"].astype(str).isin(pool)]

        # 2) 必须有本地行情
        if require_data:
            bars = self.cache / "bars"
            df = df[df["symbol"].astype(str).map(lambda s: (bars / f"{s}.parquet").exists())]

        # 3) 排除 ST / 退市
        if exclude_st:
            if "is_st" in df.columns:
                df = df[~df["is_st"].fillna(False).astype(bool)]
            name = df["name"].astype(str)
            df = df[~name.str.contains("ST|退", regex=True, na=False)]

        # 4) 次新股过滤
        if min_list_days and "list_date" in df.columns:
            asof_d = _to_date(asof) if asof else date.today()
            ld = pd.to_datetime(df["list_date"], errors="coerce")
            ok = ld.notna() & ((pd.Timestamp(asof_d) - ld).dt.days >= min_list_days)
            df = df[ok]

        syms = [str(s) for s in df["symbol"].tolist()]

        # 5) 流动性过滤（需要读行情，较慢，默认只在显式要求时做）
        if min_turnover:
            syms = self._filter_liquidity(syms, asof, min_turnover, lookback)

        # 6) 规模上限
        if max_symbols and len(syms) > max_symbols:
            syms = syms[:max_symbols]
        return syms

    # ---------------------------------------------------------------- 流动性
    def _filter_liquidity(self, symbols: list[str], asof, min_turnover: float,
                          lookback: int) -> list[str]:
        """最近 lookback 个交易日日均成交额 >= min_turnover。"""
        bars = self.cache / "bars"
        out = []
        for s in symbols:
            p = bars / f"{s}.parquet"
            if not p.exists():
                continue
            try:
                df = pd.read_parquet(p, columns=["time", "amount"])
            except Exception:  # noqa: BLE001
                continue
            if df.empty:
                continue
            if asof is not None:
                df = df[pd.to_datetime(df["time"]).dt.date <= _to_date(asof)]
            tail = df.tail(lookback)
            if len(tail) >= max(5, lookback // 4) and tail["amount"].mean() >= min_turnover:
                out.append(s)
        return out

    # ---------------------------------------------------------------- 诊断
    def describe(self, index: str | None = None) -> pd.DataFrame:
        """返回各环节的剔除统计，便于排查"为什么我的股票池是空的"。"""
        cfg_u = getattr(self.cfg, "universe", None)
        index = index if index is not None else (cfg_u.index if cfg_u else "hs300")
        steps = []
        df = self.meta.copy()
        steps.append(("全市场元数据", len(df)))

        code = INDEX_ALIAS.get(str(index).lower(), None)
        if code is not None and not self.constituents.empty:
            pool = set(self.constituents.loc[
                self.constituents["index_code"] == code, "symbol"].astype(str))
            df = df[df["symbol"].astype(str).isin(pool)]
        steps.append((f"指数池 {index}({code})", len(df)))

        bars = self.cache / "bars"
        df = df[df["symbol"].astype(str).map(lambda s: (bars / f"{s}.parquet").exists())]
        steps.append(("有本地行情", len(df)))

        if "is_st" in df.columns:
            df = df[~df["is_st"].fillna(False).astype(bool)]
            df = df[~df["name"].astype(str).str.contains("ST|退", regex=True, na=False)]
        steps.append(("剔除 ST/退市", len(df)))

        if "list_date" in df.columns:
            ld = pd.to_datetime(df["list_date"], errors="coerce")
            df = df[ld.isna() | ((pd.Timestamp(date.today()) - ld).dt.days
                                 >= (cfg_u.min_list_days if cfg_u else 60))]
        steps.append((f"上市满 {cfg_u.min_list_days if cfg_u else 60} 天", len(df)))

        return pd.DataFrame(steps, columns=["环节", "剩余数量"])


def select_universe(cfg, **kwargs) -> list[str]:  # type: ignore[no-untyped-def]
    """便捷函数。"""
    return UniverseSelector(cfg).select(**kwargs)


def _to_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
