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
    """股票池选择器。

    两种偏差的区别（重要）
    ----------------------
    - **前视偏差**：用了"当时还不知道"的未来信息。本类的指数池按
      ``asof`` 过滤 ``in_date > asof`` 的成分即可消除。
    - **幸存者偏差**：样本里只留下"活到今天"的个体，剔除了"中途被淘汰"的。
      需要**完整的历史成分**才能消除，而公开免费源拿不到沪深300 的历史
      成分（`ak.index_stock_cons` 只有"当前成分 + 纳入日期"，无剔除日期）。
      故**本类无法消除幸存者偏差**，指数池回测收益天然偏高。

    使用 ``describe()`` 可查看逐环节剔除统计。
    """

    def __init__(self, cfg=None, cache_dir: str | Path | None = None) -> None:  # type: ignore[no-untyped-def]
        self.cfg = cfg
        d = cache_dir or getattr(getattr(cfg, "data", None), "cache_dir", "data_cache")
        p = Path(d)
        self.cache = p if p.is_absolute() else PROJECT_ROOT / p
        self._meta: pd.DataFrame | None = None
        self._cons: pd.DataFrame | None = None
        #: 最近一次 ``select()`` 因 ``in_date > asof`` 被剔除的成分数（诊断用）
        self._last_dropped_after_asof: int = 0

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

        # ⚠️ 每个参数都要显式回落到配置，否则配置项会被**静默忽略**。
        # 踩过的坑：`min_turnover` 原先只在显式传参时才生效，
        # `config/*.yaml` 里写的 `universe.min_turnover` 从未被读取 ——
        # 于是"流动性过滤"实际上从没执行过，且因返回结果与不过滤时
        # 完全一致而不易察觉。
        min_list_days = (min_list_days if min_list_days is not None
                         else (cfg_u.min_list_days if cfg_u else 60))
        min_turnover = (min_turnover if min_turnover is not None
                        else (cfg_u.min_turnover if cfg_u else 0.0))
        max_symbols = (max_symbols if max_symbols is not None
                       else (cfg_u.max_symbols if cfg_u else None))
        lookback = (lookback if lookback is not None
                    else (cfg_u.lookback if cfg_u else 20))

        df = self.meta.copy()
        if df.empty:
            return []

        # 1) 指数池（**按 asof 消除前视偏差**）
        #
        # `index_constituents.parquet` 里每只成分都带 `in_date`（纳入日期）。
        # 若不看 `in_date`，回测 2023 年时会把"2024 年才被纳入指数"的股票
        # 也算进池子 —— 那是**前视偏差**（当时它根本不在指数里）。
        #
        # 实测影响：asof=2023-01-01 时，288 只里有 **78 只**是回测期内
        # 才纳入的。
        #
        # ⚠️ 本过滤**只能修前视偏差，修不了幸存者偏差**：
        # `ak.index_stock_cons` 只返回"当前成分 + 纳入日期"，不含剔除日期，
        # 「曾在指数内、后被调出」的股票根本不在文件里。故池子仍是
        # "幸存者"集合，收益仍偏高。详见报告口径说明。
        code = INDEX_ALIAS.get(str(index).lower(), None)
        if code is not None:
            cons = self.constituents
            if not cons.empty:
                sub = cons[cons["index_code"] == code]
                sub = sub.drop_duplicates(subset=["symbol"], keep="first")
                if asof is not None and "in_date" in sub.columns:
                    in_d = pd.to_datetime(sub["in_date"], errors="coerce")
                    a = pd.Timestamp(_to_date(asof))
                    # in_date 缺失时保守保留（宁可多留，不可错杀）
                    keep = in_d.isna() | (in_d <= a)
                    n_drop = int((~keep).sum())
                    if n_drop:
                        self._last_dropped_after_asof = n_drop
                    sub = sub[keep]
                pool = set(sub["symbol"].astype(str))
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
    def target_pool(
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
        """取 ``asof`` **之后**应当成为成分的那批股票（增量入池用）。

        与 :meth:`select` 的关系
        ------------------------
        ``select(asof=T)`` 是「T 时点已知的池子」——它**只增不减**，
        因为 ``index_constituents.parquet`` 里没有剔除日期。对**单点静态**
        回测这是正确的（保守口径，见类 docstring）。

        但"逐日滚动"若每天重新 ``select(asof=当日)``，池子会从 183 只
        **单调铺开**到 300 只 —— 回测 2021 年初只有 183 只可选，这是
        把「成分调整批次」误当成了「成分集合扩张」。

        本方法返回 ``select(asof) - select(asof_prev)``，即两个时点之间
        新纳入的成分。调用方把它**并进**已有池子，池子便逐期收敛到目标规模。

        ⚠️ 仍未消除幸存者偏差
        ---------------------
        「曾经在指数里、后被调出」的股票**不在文件里**，因此并进来的都是
        "最终还在指数里"的幸存者。本方法只能让**规模**随时间对了，
        不能消除**选样**的幸存者偏差。两者的区别见类 docstring。
        """
        cur = set(self.select(
            index=index, asof=asof, exclude_st=exclude_st,
            min_list_days=min_list_days, min_turnover=min_turnover,
            lookback=lookback, max_symbols=max_symbols, require_data=require_data,
        ))
        if asof is None:
            return sorted(cur)

        # 上一个批次时点：直接取成分表里「严格早于本时点」的最大 in_date。
        # 用它比"上一个自然年"更贴近真实的调整批次（沪深300 每年 6/12 月
        # 各调一次，in_date 恰好落在调整生效日）。
        code = INDEX_ALIAS.get(str(
            index if index is not None
            else getattr(getattr(self.cfg, "universe", None), "index", "hs300")
        ).lower())
        if code is None:
            return sorted(cur)

        cons = self.constituents
        if cons.empty or "in_date" not in cons.columns:
            return sorted(cur)
        sub = cons[cons["index_code"] == code].drop_duplicates(subset=["symbol"], keep="first")
        in_d = pd.to_datetime(sub["in_date"], errors="coerce").dropna()
        a = pd.Timestamp(_to_date(asof))
        earlier = in_d[in_d < a]
        if earlier.empty:
            return sorted(cur)

        prev = self.select(
            index=index, asof=earlier.max(), exclude_st=exclude_st,
            min_list_days=min_list_days, min_turnover=min_turnover,
            lookback=lookback, max_symbols=max_symbols, require_data=require_data,
        )
        return sorted(cur - set(prev))

    def describe(self, index: str | None = None, asof=None) -> pd.DataFrame:
        """返回各环节的剔除统计，便于排查"为什么我的股票池是空的"。

        传 ``asof`` 时会展示前视偏差修正（剔除 ``in_date > asof`` 的成分）
        这一环节。**注意它不消除幸存者偏差** —— 见类 docstring。
        """
        cfg_u = getattr(self.cfg, "universe", None)
        index = index if index is not None else (cfg_u.index if cfg_u else "hs300")
        steps = []
        df = self.meta.copy()
        steps.append(("全市场元数据", len(df)))

        code = INDEX_ALIAS.get(str(index).lower(), None)
        if code is not None and not self.constituents.empty:
            sub = self.constituents[self.constituents["index_code"] == code]
            n_raw = len(sub)
            sub = sub.drop_duplicates(subset=["symbol"], keep="first")
            steps.append((f"指数池 {index}({code}) 原始/去重",
                          f"{n_raw}/{len(sub)}"))
            if asof is not None and "in_date" in sub.columns:
                in_d = pd.to_datetime(sub["in_date"], errors="coerce")
                keep = in_d.isna() | (in_d <= pd.Timestamp(_to_date(asof)))
                steps.append(("剔除 asof 后才纳入(前视偏差)", int((~keep).sum())))
                sub = sub[keep]
            pool = set(sub["symbol"].astype(str))
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
            asof_ts = pd.Timestamp(_to_date(asof)) if asof else pd.Timestamp(date.today())
            df = df[ld.isna() | ((asof_ts - ld).dt.days
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
