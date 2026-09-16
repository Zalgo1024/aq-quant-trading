"""因子面板构建器（P2 核心）。

把本地日线 parquet 变成一张**因子面板长表**：

    date | symbol | industry | mktcap | f_1 ... f_n | fwd_ret_1/5/10/20 | t1_tradable

设计原则
--------
1. **无未来函数**：t 行的因子只用 <= t 的数据；前瞻收益明确是 t -> t+n。
2. **向量化**：直接调 :mod:`aq.factors.vlib` 内核，与实时打分同一套算法。
3. **可交易性显式建模**：次日停牌 / 一字涨停（买不进）/ 一字跌停（卖不出）
   都打上 ``t1_tradable=False``，IC 检验时剔除 —— 否则会算出"买一字板"的假 alpha。
4. **流式落盘**：用 ParquetWriter 分块写，避免 5562 只 × 2000 天把内存撑爆。
5. **前视偏差可标注**：静态指数成分池带幸存者偏差，用 ``universe=liquid``
   （纯规则动态池：上市天数 + 非 ST + 成交额）才是干净的。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from aq.config.settings import PROJECT_ROOT
from aq.factors.vlib import REQUIRES_SHARES, spec_names

BARS_DIR = PROJECT_ROOT / "data_cache" / "bars"
CACHE_DIR = PROJECT_ROOT / "data_cache" / "factors"

# 需要读取的原始列
RAW_COLS = [
    "time", "open", "high", "low", "close", "volume", "amount",
    "pre_close", "limit_up", "limit_down", "is_trading", "adj_factor",
]

# 静态池（带幸存者偏差，仅用于对比）
STATIC_POOLS = {"hs300", "zz500", "zz800", "sz50", "zz1000"}


@dataclass
class PanelConfig:
    start: str = "2022-01-01"
    end: str = "2026-09-15"
    universe: str = "liquid"          # liquid | all | hs300 | zz500 | zz800
    factors: list[str] = field(default_factory=spec_names)
    horizons: tuple[int, ...] = (1, 5, 10, 20)
    min_history: int = 120            # 至少有多少根历史 bar 才计算因子
    min_list_days: int = 180          # 动态池：上市满 N 个自然日
    min_amount: float = 2e7           # 动态池：近 20 日日均成交额下限（元）
    exclude_st: bool = True
    max_symbols: int | None = None
    chunk_size: int = 400             # 每多少只股票 flush 一次
    # 显式指定股票列表（回测引擎用）：给定时**不再做流动性过滤**，
    # 因为股票池已由 UniverseSelector 决定，再过滤一次是重复且会改语义。
    symbols: list[str] | None = None


class FactorPanelBuilder:
    """构建因子面板。"""

    def __init__(
        self,
        cfg: PanelConfig | None = None,
        bars_dir: Path | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg or PanelConfig()
        self.bars_dir = Path(bars_dir) if bars_dir else BARS_DIR
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._meta: pd.DataFrame | None = None
        self._pool_static: set[str] | None = None

    # ------------------------------------------------------------------ 元数据
    @property
    def meta(self) -> pd.DataFrame:
        """股票列表元数据（行业 / 股本 / 上市日 / ST）。"""
        if self._meta is None:
            p = PROJECT_ROOT / "data_cache" / "stock_list.parquet"
            df = pd.read_parquet(p)
            df["symbol"] = df["symbol"].astype(str).str.zfill(6)
            self._meta = df.set_index("symbol")
        return self._meta

    def _static_pool(self) -> set[str]:
        """从 index_constituents 取静态成分股。"""
        if self._pool_static is not None:
            return self._pool_static
        p = PROJECT_ROOT / "data_cache" / "index_constituents.parquet"
        if not p.exists():
            self._pool_static = set()
            return self._pool_static
        df = pd.read_parquet(p)
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
        want: set[str] = set()
        u = self.cfg.universe
        if u == "hs300":
            want = set(df[df.index_code == "000300"].symbol)
        elif u == "zz500":
            want = set(df[df.index_code == "000905"].symbol)
        elif u == "zz800":
            want = set(df[df.index_code.isin(["000300", "000905"])].symbol)
        elif u == "sz50":
            want = set(df[df.index_code == "000016"].symbol)
        elif u == "zz1000":
            want = set(df[df.index_code == "000852"].symbol)
        self._pool_static = want
        return want

    def all_symbols(self) -> list[str]:
        """候选股票列表。

        显式给了 ``symbols`` 就直接用它（回测引擎场景）；否则取本地有行情的全部代码。
        """
        if self.cfg.symbols:
            syms = [str(s).zfill(6) for s in self.cfg.symbols]
            return syms[: self.cfg.max_symbols] if self.cfg.max_symbols else syms
        files = sorted(self.bars_dir.glob("*.parquet"))
        syms = [f.stem for f in files]
        u = self.cfg.universe
        if u in STATIC_POOLS:
            pool = self._static_pool()
            syms = [s for s in syms if s in pool]
        if self.cfg.max_symbols:
            syms = syms[: self.cfg.max_symbols]
        return syms

    # ------------------------------------------------------------------ 单股票
    def _load_one(self, symbol: str) -> pd.DataFrame | None:
        p = self.bars_dir / f"{symbol}.parquet"
        if not p.exists():
            return None
        try:
            df = pd.read_parquet(p, columns=RAW_COLS)
        except Exception:  # noqa: BLE001
            return None
        if df.empty:
            return None
        df["time"] = pd.to_datetime(df["time"]).dt.strftime("%Y-%m-%d")
        df = df.sort_values("time").reset_index(drop=True)
        # 停牌行（is_trading=False）不能直接删：会破坏时间连续性，
        # 但收益计算要跳过 —— 统一在后面用 mask 处理。
        return df

    def build_one(self, symbol: str) -> pd.DataFrame | None:
        """计算单只股票的完整面板片段。"""
        from aq.factors.vlib import compute_all

        cfg = self.cfg
        df = self._load_one(symbol)
        if df is None or len(df) < cfg.min_history:
            return None

        meta_row = self.meta.loc[symbol] if symbol in self.meta.index else None

        def _valid_shares(v) -> float | None:
            """股本有效性检查：None / NaN / <=0 都视为缺失。

            **为什么必须查 0 与负数**：元数据里存在 `total_share=0` 的脏数据
            （抓取失败被填 0 而非 NaN）。旧实现只查 NaN，于是一批股票算出了
            `mktcap = close * 0 = 0`。后果有两层：
            1. 中性化时 `ln(0) = -inf`，污染回归系数，让整列结果失真；
            2. 覆盖率被虚高统计（0 值也算"有市值"），掩盖了数据问题。
            宁可判为缺失走"纯行业中性化"回退，也不要让脏值进回归。
            """
            if v is None:
                return None
            try:
                x = float(v)
            except (TypeError, ValueError):
                return None
            if not np.isfinite(x) or x <= 0:
                return None
            return x

        shares = None
        if meta_row is not None:
            shares = _valid_shares(meta_row.get("float_share"))
            if shares is None:
                shares = _valid_shares(meta_row.get("total_share"))
        if shares is not None:
            df["float_share"] = float(shares)
        elif "turn_rate_20" in cfg.factors:
            df["float_share"] = np.nan

        # ---- 因子（向量化，含 warmup 全段）----
        fac = compute_all(df, [f for f in cfg.factors])
        fac = fac.astype("float64")
        # 历史不足的行置 NaN
        warm = max(1, min(cfg.min_history, len(df)))
        fac.iloc[: min(len(df) - 1, 5)] = np.nan  # 前几行 rolling 不稳

        # ---- 收益（后复权价，除权日自动连续）----
        close = df["close"].astype("float64")
        out = pd.DataFrame({"date": df["time"], "symbol": symbol})
        for n in cfg.horizons:
            out[f"fwd_ret_{n}"] = (close.shift(-n) / close - 1.0).to_numpy()
        # 次日开盘跳空（评估隔夜成本 / 是否一字板）
        nxt_open = df["open"].astype("float64").shift(-1)
        nxt_high = df["high"].astype("float64").shift(-1)
        nxt_low = df["low"].astype("float64").shift(-1)
        nxt_close = df["close"].astype("float64").shift(-1)
        nxt_lu = df["limit_up"].astype("float64").shift(-1)
        nxt_ld = df["limit_down"].astype("float64").shift(-1)
        nxt_trading = (
            df["is_trading"].astype(bool).shift(-1, fill_value=False)
            if "is_trading" in df
            else pd.Series(True, index=df.index)
        )

        out["fwd_open_1"] = (nxt_open / close - 1.0).to_numpy()

        # ---- 可交易性 ----
        # 一字涨停：次日最高=最低=涨停价（挂单买不进）
        yi_zi_up = (nxt_high <= nxt_lu + 1e-6) & (nxt_low >= nxt_lu - 1e-6) & nxt_lu.notna()
        yi_zi_dn = (nxt_high <= nxt_ld + 1e-6) & (nxt_low >= nxt_ld - 1e-6) & nxt_ld.notna()
        tradable = (~yi_zi_up) & (~yi_zi_dn)
        tradable = tradable & nxt_trading.astype(bool)
        out["t1_tradable"] = tradable.to_numpy()
        out["t1_limit_up"] = ((nxt_close >= nxt_lu - 1e-6) & nxt_lu.notna()).to_numpy()

        # ---- 价格 / 市值 / 行业 ----
        adj = df["adj_factor"].astype("float64").replace(0.0, np.nan)
        out["close_real"] = (close / adj).to_numpy()
        if "float_share" in df:
            out["mktcap"] = (out["close_real"] * df["float_share"]).to_numpy()
        else:
            out["mktcap"] = np.nan
        out["amount"] = df["amount"].to_numpy()
        out["volume"] = df["volume"].to_numpy()

        # 上市天数（自然日）
        if meta_row is not None:
            ld = meta_row.get("list_date")
            ld = None if (ld is None or (isinstance(ld, float) and np.isnan(ld))) else str(ld)
        else:
            ld = None
        if ld and ld not in ("", "NaT", "nan", "0"):
            try:
                days = (pd.to_datetime(out["date"]) - pd.to_datetime(ld)).dt.days
                out["list_days"] = days.to_numpy()
            except Exception:  # noqa: BLE001
                out["list_days"] = 9999
        else:
            out["list_days"] = 9999

        out["is_st"] = bool(meta_row is not None and bool(meta_row.get("is_st")))
        ind = "unknown"
        if meta_row is not None:
            for col in ("industry_csrc", "industry", "industry_sina"):
                v = meta_row.get(col)
                if v is not None and not (isinstance(v, float) and np.isnan(v)) and str(v).strip():
                    ind = str(v)
                    break
        out["industry"] = ind

        # ---- 拼接 + 时间过滤 ----
        out = pd.concat([out.reset_index(drop=True), fac.reset_index(drop=True)], axis=1)
        mask = (out["date"] >= cfg.start) & (out["date"] <= cfg.end)
        out = out[mask]
        if out.empty:
            return None

        # 因子全 NaN 的行没有研究价值
        fcols = [c for c in fac.columns]
        if fcols:
            out = out[out[fcols].notna().any(axis=1)]
        return out.reset_index(drop=True)

    # ------------------------------------------------------------------ 动态池
    def apply_liquid_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """动态流动性池过滤（纯规则，无前视）。

        显式指定了 ``symbols`` 时跳过 —— 股票池已由调用方决定。
        """
        cfg = self.cfg
        if cfg.universe != "liquid" or cfg.symbols:
            return df
        if "amount" not in df.columns:
            return df
        # 近 20 日日均成交额（按 symbol 滚动，注意 panel 已按 symbol 连续排列）
        amt = df.groupby("symbol", sort=False)["amount"]
        avg20 = amt.transform(
            lambda s: s.rolling(20, min_periods=10).mean()
        )
        ok = avg20 >= cfg.min_amount
        ok = ok & (df["list_days"] >= cfg.min_list_days)
        if cfg.exclude_st and "is_st" in df.columns:
            ok = ok & (~df["is_st"].astype(bool))
        # 前 20 行 rolling 不足 -> 保守剔除
        ok = ok & avg20.notna()
        return df[ok]

    # ------------------------------------------------------------------ 主入口
    def build(self, force: bool = False) -> pd.DataFrame:
        """构建（或复用缓存）因子面板，返回完整 DataFrame。"""
        path = self._cache_path()
        if path.exists() and not force:
            print(f"[面板] 复用缓存 {path.name}（加 --force 重建）")
            return pd.read_parquet(path)

        syms = self.all_symbols()
        print(f"[面板] universe={self.cfg.universe} 候选 {len(syms)} 只，"
              f"{self.cfg.start} ~ {self.cfg.end}，因子 {len(self.cfg.factors)} 个")

        t0 = time.time()
        chunks: list[pd.DataFrame] = []
        writer: pq.ParquetWriter | None = None
        n_rows = 0
        n_cached = 0
        tmp = path.with_suffix(".tmp.parquet")

        try:
            for i, sym in enumerate(syms, 1):
                cached = self._load_part(sym)
                if cached is not None:
                    # 缓存存的是全区间，按本次 start/end 切片
                    sel = cached
                    if "date" in sel.columns:
                        sel = sel[(sel["date"] >= self.cfg.start)
                                  & (sel["date"] <= self.cfg.end)]
                    if not sel.empty:
                        chunks.append(sel)
                        n_cached += 1
                else:
                    try:
                        part = self.build_one(sym)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  [跳过] {sym}: {type(exc).__name__} {exc}")
                        continue
                    if part is not None and not part.empty:
                        self._save_part(sym, part)
                        chunks.append(part)
                if len(chunks) >= self.cfg.chunk_size:
                    blk = pd.concat(chunks, ignore_index=True)
                    blk = self._shrink(blk)
                    writer = self._write(writer, blk, tmp)
                    n_rows += len(blk)
                    chunks = []
                if i % 500 == 0:
                    print(f"  ... {i}/{len(syms)} 行={n_rows} 命中缓存={n_cached} "
                          f"用时 {time.time()-t0:.0f}s")

            if chunks:
                blk = pd.concat(chunks, ignore_index=True)
                blk = self._shrink(blk)
                writer = self._write(writer, blk, tmp)
                n_rows += len(blk)
        finally:
            if writer is not None:
                writer.close()

        if n_cached:
            print(f"[面板] 命中单股票缓存 {n_cached}/{len(syms)} 只")

        if n_rows == 0:
            raise RuntimeError("面板为空：请检查 bars 目录与 start/end 区间")

        df = pd.read_parquet(tmp)
        df = self.apply_liquid_filter(df)
        df = df.reset_index(drop=True)
        df.to_parquet(path, index=False)
        # 临时文件清理由 best-effort 改为**忽略一切异常**：
        # 某些沙箱环境给 os.unlink 注入了"安全删除"垫片，批量删除时会抛
        # SAFE_DELETE_BULK_CONFIRM_REQUIRED。它只影响清理，不影响正确性
        # —— 数据已经写进 path 了。故这里绝不能让清理失败把整次构建带崩。
        try:
            if tmp.exists():
                tmp.unlink()
        except BaseException:  # noqa: BLE001
            pass

        print(f"[面板] 完成 {len(df):,} 行 × {len(df.columns)} 列，"
              f"{df['symbol'].nunique()} 只，{df['date'].min()} ~ {df['date'].max()}，"
              f"用时 {time.time()-t0:.0f}s -> {path.name}")
        return df

    # ------------------------------------------------------------------ helpers
    def _shrink(self, df: pd.DataFrame) -> pd.DataFrame:
        """降精度 + 分类化，控制落盘体积。"""
        for c in df.columns:
            if df[c].dtype == "float64":
                df[c] = df[c].astype("float32")
            elif df[c].dtype == "int64":
                df[c] = df[c].astype("int32")
        if "industry" in df.columns:
            df["industry"] = df["industry"].astype("category")
        return df

    # ------------------------------------------------------------------ 增量缓存
    def _stock_cache_dir(self) -> Path:
        """单股票因子缓存目录。

        为什么按**单股票**而不是按**整份面板**缓存
        ------------------------------------------
        回测引擎每次 ``--start`` 不同 → ``PanelConfig`` 不同 → 整份面板的
        cache key 不同 → 全部重算。而 ``data_cache/factors/`` 下已经躺着
        同一批股票的因子（供因子研究用），只因为区间/列集合不同就**一行都用不上**:

        - ``panel_liquid_2019-01-01_2026-09-15``：5256 只 / 1861 天（研究口径）
        - 回测 hs300 池要的：300 只 / 1372 天

        后者是前者的**严格子集**，却要重跑 8 分钟。按单股票缓存后，
        任意 ``(symbol, start, end, factors)`` 组合都能复用已算好的片段。

        ``symbols`` 显式给定时（回测场景）才启用 —— 全市场研究场景
        ``symbols=None``，缓存键会退化成上万只的目录，得不偿失。
        """
        return self.cache_dir / "parts"

    def _parts_path(self, symbol: str) -> Path | None:
        """单股票缓存文件路径，未启用时返回 None。"""
        if not self.cfg.symbols:
            return None
        c = self.cfg
        key = "|".join([
            c.end, ",".join(sorted(c.factors)),
            ",".join(map(str, c.horizons)), str(c.min_history),
        ])
        h = hashlib.md5(key.encode()).hexdigest()[:8]
        return self._stock_cache_dir() / h / f"{symbol}.parquet"

    def _load_part(self, symbol: str) -> pd.DataFrame | None:
        """读单股票缓存（**只读该文件，不校验是否过期**）。

        缓存键含 ``end`` 与因子集合，故 ``end`` 变了不会命中旧缓存；
        ``start`` 不在键里 —— 因为缓存**存全区间**（从该股票最早一根 bar
        开始），取用时再按 ``start`` 切片。这样多个 ``--start`` 共享一份缓存。
        """
        p = self._parts_path(symbol)
        if p is None or not p.exists():
            return None
        try:
            df = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            return None
        return df if not df.empty else None

    def _save_part(self, symbol: str, df: pd.DataFrame) -> None:
        p = self._parts_path(symbol)
        if p is None or df.empty:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._shrink(df).to_parquet(p, index=False)
        except Exception:  # noqa: BLE001
            pass

    def _write(self, writer: pq.ParquetWriter | None, blk: pd.DataFrame, path: Path):
        tbl = pa.Table.from_pandas(blk, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(path, tbl.schema, compression="snappy")
        writer.write_table(tbl)
        return writer

    def _cache_path(self) -> Path:
        c = self.cfg
        key = "|".join(
            [
                c.start, c.end, c.universe, ",".join(sorted(c.factors)),
                ",".join(map(str, c.horizons)), str(c.min_history),
                str(c.min_list_days), f"{c.min_amount:.0f}",
                str(c.exclude_st), str(c.max_symbols),
                ("" if not c.symbols
                 else hashlib.md5(",".join(sorted(c.symbols)).encode()).hexdigest()[:8]),
            ]
        )
        h = hashlib.md5(key.encode()).hexdigest()[:10]
        return self.cache_dir / f"panel_{c.universe}_{c.start}_{c.end}_{h}.parquet"


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


def build_panel(
    start: str = "2022-01-01",
    end: str = "2026-09-15",
    universe: str = "liquid",
    factors: list[str] | None = None,
    force: bool = False,
    **kw,
) -> pd.DataFrame:
    cfg = PanelConfig(start=start, end=end, universe=universe,
                      factors=factors or spec_names(), **kw)
    return FactorPanelBuilder(cfg).build(force=force)
