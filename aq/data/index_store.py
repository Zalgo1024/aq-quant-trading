"""指数行情存储（与个股行情**物理隔离**）。

为什么必须单独一个目录
----------------------
A 股存在**指数代码与深市个股代码撞车**：

===========  ================  ==========================
代码          指数语义           深市个股（同代码）
===========  ================  ==========================
``000001``    上证指数           平安银行
``000016``    上证50              *ST康佳A
``000852``    中证1000           石化机械
``000905``    中证500            厦门港务
===========  ================  ==========================

``data_cache/bars/`` 里上述 4 个文件名存的**全部是个股**（实测：000905 的
close≈28，那是厦门港务的股价，不是中证500 的 ~6000 点）。若有人把
``bars["000905"]`` 当基准用，得到的是单只个股走势，基准/超额/信息比率会
全部错得毫无察觉。

因此本模块把指数行情放在 ``data_cache/index_bars/``，并**在存储层就强制**
区分：

- 目录隔离：``index_bars/`` 下**只允许**存在指数，不允许任何个股；
- 文件名带市场后缀：``000300.SH.parquet`` / ``000905.SH.parquet``，
  从文件名即可看出这是指数代码而非「6 位裸代码」的个股；
- 读取时校验 ``kind == "index"``：老文件或误放的文件会显式报错。

基准数据来源与口径
------------------
指数**不复权**（没有分红送转概念），因此不写 ``adj_factor``；``close`` 即
指数点位。字段与个股 bars 尽量对齐（time/open/high/low/close/volume/amount）
以便复用同一套 K 线读取代码，但**多一列 ``kind``** 用于自证身份。

用法::

    from aq.data.index_store import IndexBarStore

    store = IndexBarStore()
    df = store.load("000300.SH")            # 沪深300 全历史
    ret = store.daily_return("000300.SH", "2023-01-01", "2024-01-01")
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from aq.config.settings import PROJECT_ROOT

#: 支持的指数：文件主名 → (中文名, akshare symbol, 起始点位参考区间)
#:
#: 主名统一带市场后缀（``.SH`` / ``.SZ``），从命名上就与个股 6 位裸代码区分开。
INDEX_UNIVERSE: dict[str, dict[str, str]] = {
    "000300.SH": {"name": "沪深300", "ak": "sh000300"},
    "000905.SH": {"name": "中证500", "ak": "sh000905"},
    "000016.SH": {"name": "上证50", "ak": "sh000016"},
    "000852.SH": {"name": "中证1000", "ak": "sh000852"},
    "399006.SZ": {"name": "创业板指", "ak": "sz399006"},
    "000001.SH": {"name": "上证指数", "ak": "sh000001"},
}

#: 「裸代码 → 指数主名」的别名表。
#:
#: config 里习惯写 ``benchmark: "000300"``，这里负责把裸代码解析成带后缀的
#: 主名。**故意只收录指数代码，不收录任何个股代码**；若传入 ``000905``
#: 会解析成中证500（而非厦门港务），这正是本模块要消除的歧义。
_BARE_ALIAS: dict[str, str] = {
    "000300": "000300.SH",
    "000905": "000905.SH",
    "000016": "000016.SH",
    "000852": "000852.SH",
    "399006": "399006.SZ",
    # 注意：000001 有歧义（上证指数 vs 平安银行），故**不**自动解析，
    # 必须显式写 "000001.SH"。bare 形式一律拒绝，避免踩坑。
}


class IndexDataMissing(RuntimeError):
    """指数行情尚未落盘。调用方应显式降级为「未接基准」，而不是静默当 0。"""


def default_index_dir() -> Path:
    return PROJECT_ROOT / "data_cache" / "index_bars"


def resolve(name: str) -> str:
    """把 ``000300`` / ``000300.SH`` / ``sh000300`` 统一成 ``000300.SH``。

    无法解析时抛 ``KeyError``（**不猜测**）—— 宁可报错也不要错误地
    把个股当指数。
    """
    raw = str(name).strip().upper()
    if not raw:
        raise KeyError("指数代码为空")

    # sh000300 / sz399006 形式
    if raw.startswith(("SH", "SZ")) and raw[2:].isdigit():
        return f"{raw[2:]}.{raw[:2]}"

    # 已带后缀
    if "." in raw:
        code, _, mkt = raw.partition(".")
        code = code.zfill(6)
        if mkt not in ("SH", "SZ"):
            raise KeyError(f"未知市场后缀：{name}")
        return f"{code}.{mkt}"

    # 裸代码
    bare = raw.zfill(6)
    if bare in _BARE_ALIAS:
        return _BARE_ALIAS[bare]
    if bare == "000001":
        raise KeyError(
            "000001 有歧义（上证指数 / 平安银行），请显式写成 '000001.SH' 表示指数"
        )
    raise KeyError(
        f"未登记的指数代码 {name!r}。已知：{', '.join(sorted(INDEX_UNIVERSE))}"
    )


class IndexBarStore:
    """``data_cache/index_bars/`` 的读写封装。"""

    #: 本目录下允许出现的列（kind 用于自证「这是指数」）
    REQUIRED_COLS = ("time", "open", "high", "low", "close")

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else default_index_dir()
        if not self.root.is_absolute():
            self.root = PROJECT_ROOT / self.root

    # ---------------------------------------------------------------- 路径
    def path_of(self, name: str) -> Path:
        return self.root / f"{resolve(name)}.parquet"

    def has(self, name: str) -> bool:
        return self.path_of(name).exists()

    def available(self) -> list[str]:
        """已落盘的指数主名（按文件名排序）。"""
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.parquet") if p.stat().st_size > 0)

    # ---------------------------------------------------------------- 读取
    def load(self, name: str, start=None, end=None) -> pd.DataFrame:
        """读指数日线。缺数据抛 ``IndexDataMissing``（**不返回空表**）。

        返回列：``time / open / high / low / close / volume / amount / kind``
        （``volume``/``amount`` 视数据源而定，缺失时补 NaN）。
        """
        p = self.path_of(name)
        if not p.exists():
            raise IndexDataMissing(
                f"指数 {resolve(name)} 行情缺失：{p} 不存在。"
                f"请先运行 python scripts/fetch_index_bars.py"
            )
        df = pd.read_parquet(p)
        if df.empty:
            raise IndexDataMissing(f"指数 {resolve(name)} 行情为空文件：{p}")

        # 自证身份：防止把个股 parquet 误放进本目录
        if "kind" in df.columns:
            kinds = set(df["kind"].dropna().unique().tolist())
            if kinds and kinds != {"index"}:
                raise ValueError(
                    f"{p.name} 的 kind={kinds} 不是 index —— "
                    f"疑似个股数据误放进了 index_bars/，请检查后再用"
                )
        else:
            # 老文件没有 kind 列时，退一步用「无 adj_factor 且非 6 位裸名」判断
            if "adj_factor" in df.columns:
                raise ValueError(
                    f"{p.name} 含 adj_factor 列 —— 指数不复权，疑似个股数据误放"
                )

        df["time"] = pd.to_datetime(df["time"])
        if start is not None:
            df = df[df["time"] >= pd.Timestamp(_as_ts(start))]
        if end is not None:
            df = df[df["time"] <= pd.Timestamp(_as_ts(end))]
        for c in self.REQUIRED_COLS:
            if c not in df.columns:
                raise ValueError(f"{p.name} 缺列 {c}")
        return df.sort_values("time").reset_index(drop=True)

    def close_series(self, name: str, start=None, end=None) -> pd.Series:
        """以日期为索引的收盘点位序列（基准对齐用）。"""
        df = self.load(name, start, end)
        return pd.Series(df["close"].values, index=df["time"].dt.date.values, name="benchmark")

    def daily_return(self, name: str, start=None, end=None) -> pd.Series:
        """日收益率序列（按日期索引）。基准超额与信息比率的输入。"""
        close = self.close_series(name, start, end)
        return close.pct_change().dropna()

    # ---------------------------------------------------------------- 写入
    def save(self, name: str, df: pd.DataFrame) -> Path:
        """落盘指数日线。强制打上 ``kind='index'`` 并清除个股专属列。"""
        out = df.copy()
        if "time" not in out.columns:
            raise ValueError("指数日线必须有 time 列")
        out["time"] = pd.to_datetime(out["time"])
        # 指数不复权：丢掉个股专属列，避免后续被误用
        out = out.drop(columns=[c for c in ("adj_factor", "limit_up", "limit_down",
                                            "pre_close", "is_trading", "symbol")
                                if c in out.columns])
        out["kind"] = "index"
        out["index_code"] = resolve(name)
        out = out.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)

        self.root.mkdir(parents=True, exist_ok=True)
        p = self.path_of(name)
        tmp = p.with_suffix(p.suffix + ".tmp")
        out.to_parquet(tmp, index=False)
        try:
            os.replace(tmp, p)
        except OSError:
            import shutil

            shutil.copyfile(tmp, p)
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return p


def _as_ts(v) -> str:
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    return str(v)[:10]
