"""ETF 行情/分红/清单的存储层（与个股、指数**物理隔离**）。

为什么需要又一个 Store
----------------------
已经有 ``BarStore``（个股，``data_cache/bars/``）和 ``IndexBarStore``
（指数，``data_cache/index_bars/``）。ETF 只多不少：

- **ETF 是投资标的**（不像指数只是基准）→ 必须保留 ``symbol`` 列。
  ``IndexBarStore.save`` 会**强制删掉** ``symbol``，所以不能直接复用。
- **ETF 的价格序列不是收益序列** → 两条独立的污染源：
  1. **分红除权**：实测拖累 1.7~3.2pp/年（``probe_etf_total_return.py``），
     量级等于整个组合的预期收益；
  2. **份额折算（拆分/合并）**：512890 在 2021-10-25 因 1:2 拆分单日
     "跌" −51.13%，使 7.7 年年化从 ≈12.4% 变成 2.32%（**差 10pp/年**）。
  → 收益一律走 ``EtfNavStore.total_return``（官方日增长率），**不要**从价格算。
  折算检测（``aq.data.etf_actions``）用于**价格路径**与数据质量告警。
- **ETF 不能被"代码段规则"识别** → 成员关系必须来自来源清单
  （``fund_etf_spot_em`` / ``fund_etf_category_sina``）。
  反例：``510080``/``510081``（2004 年成立）、``560002``（2006）、
  ``560003``（2007）代码都落在 ETF 段，却是**普通开放式基金**。

⚠️ 池口径警告
-------------
免费源拿不到已退市/已清盘 ETF 的清单与行情（腾讯 ``IndexError``、
新浪空；东财名录不含已终止产品）。因此本模块的清单**天然带单向幸存者偏差**：

    universe_caliber = "live_only_biased"

任何基于本模块算出的**绝对收益**都不可引用，只有"相对同池等权"的超额有意义。
偏差上界见 ``scripts/probes/probe_etf_survivorship.py``。

用法::

    from aq.data.etf_store import EtfBarStore, EtfNavStore, load_etf_list

    bars = EtfBarStore()
    df = bars.load("510300")                 # 510300.SH 的日线，**价格口径**
    rt = EtfNavStore().total_return("510300")  # ⭐ 权威全收益（官方日增长率）
    rt2 = bars.total_return("510300")        # 同上（自动优先走净值，退化才用价格+分红）

    lst = load_etf_list()                    # point-in-time 清单（只含现役）
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from aq.config.settings import PROJECT_ROOT

#: 自描述标记：本模块产出的所有东西都建立在**只含现役**的池子上。
#: 写进产物 json，避免下游误把绝对收益当无偏数字引用。
UNIVERSE_CALIBER = "live_only_biased"

#: 价格口径标记（撮合与信号用价格；收益归因须换全收益，见 ``total_return``）。
PRICE_CALIBER_RAW = "raw_no_dividend"
#: 份额折算还原后的价格口径：``close × split_factor``。**只修折算、不含分红**。
#: 等价于真实持有人经历拆分后（份额翻倍、价格减半）的连续序列。
PRICE_CALIBER_SPLIT_ADJ = "split_adjusted_no_dividend"
#: 全收益口径：份额还原价 + 现金分红（**分红不再投资**，按除息日计入现金）。
#: ⚠️ 早期版本把这个常量命名为 ``..._dividend_reinvested``，与实现不符 ——
#: 实现是 ``r = (P_adj + ΔD) / P_adj_prev − 1``，即分红按现金计、不滚入。
PRICE_CALIBER_TOTAL = "total_return_dividend_cash_no_reinvest"
#: ⭐ **权威全收益口径**：基金公司公布的官方「日增长率」（小数）。
#: 含分红、份额折算中性、无单位歧义。见 ``EtfNavStore.total_return``。
NAV_CALIBER_TOTAL = "nav_official_daily_growth_total_return"
#: ⚠️ **不是**全收益：累计净值比率。它被累计分红稀释（``r/(1+D/U)``），
#: 且在份额折算日可能被重述。仅作诊断与退化兜底。
NAV_CALIBER_CUM_RATIO_DILUTED = "cum_nav_ratio_diluted_not_total_return"


class EtfDataMissing(RuntimeError):
    """ETF 行情尚未落盘。调用方应显式降级，而不是静默当 0。"""


# ---------------------------------------------------------------------------
# 代码规范化
# ---------------------------------------------------------------------------

def market_of(code: str) -> str:
    """由基金代码推断交易所（``SH`` / ``SZ``）。

    ⚠️ 注意这与「用代码段识别 ETF」**是两件事**：
    - 用代码段判断"这是不是 ETF" → **禁止**（反例见模块 docstring）；
    - 用首位数字判断"它在哪个交易所" → 可靠。基金代码沪市 ``5xxxxx``
      （510/511/512/513/515/516/517/518/520/560-563/588/589），
      深市 ``1xxxxx``（159xxx）。这条规则与"是不是 ETF"无关。
    """
    raw = str(code).strip().upper()
    for pfx, mkt in (("SH", "SH"), ("SZ", "SZ")):
        if raw.startswith(pfx):
            raw = raw[2:]
            return market_of(raw) if raw else mkt
    raw = raw.split(".")[0].zfill(6)
    if not raw.isdigit():
        raise KeyError(f"非法基金代码：{code!r}")
    if raw.startswith("1"):
        return "SZ"
    if raw.startswith("5"):
        return "SH"
    raise KeyError(
        f"{code!r} 不在 ETF 可能的交易所段内（沪 5xxxxx / 深 1xxxxx）。"
        f"它可能根本不是 ETF —— 请用来源清单确认，不要靠代码猜。"
    )


def resolve(code: str) -> str:
    """把 ``510300`` / ``sh510300`` / ``510300.SH`` 统一成 ``510300.SH``。"""
    raw = str(code).strip().upper()
    if raw.endswith((".SH", ".SZ")):
        body, _, mkt = raw.partition(".")
        return f"{body.zfill(6)}.{mkt}"
    return f"{raw.split('.')[0].lstrip('SHSZ').zfill(6) if raw.startswith(('SH', 'SZ')) else raw.split('.')[0].zfill(6)}.{market_of(raw)}"


def default_etf_dir() -> Path:
    return PROJECT_ROOT / "data_cache" / "etf_bars"


def default_list_path() -> Path:
    return PROJECT_ROOT / "data_cache" / "etf_list.parquet"


def default_dividend_path() -> Path:
    return PROJECT_ROOT / "data_cache" / "etf_dividends.parquet"


def default_nav_dir() -> Path:
    """净值目录：``data_cache/etf_nav/``（逐只一个 parquet）。"""
    return PROJECT_ROOT / "data_cache" / "etf_nav"


def _as_ts(v) -> str:
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    return str(v)[:10]


def _atomic_write(df: pd.DataFrame, path: Path) -> Path:
    """先写 .tmp 再落位。

    ``os.replace`` 在目标文件被其他进程持有句柄时会 WinError 5
    （IDE 的文件监听/预览会抓读句柄），所以保留 ``copyfile`` 兜底 ——
    这与 ``IndexBarStore.save`` 是同一套已验证写法。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    try:
        os.replace(tmp, path)
    except OSError:
        import shutil

        shutil.copyfile(tmp, path)
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return path


# ---------------------------------------------------------------------------
# 行情
# ---------------------------------------------------------------------------

class EtfBarStore:
    """``data_cache/etf_bars/`` 的读写封装（**价格口径**日线）。"""

    REQUIRED_COLS = ("time", "open", "high", "low", "close")

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else default_etf_dir()
        if not self.root.is_absolute():
            self.root = PROJECT_ROOT / self.root

    # ---------------------------------------------------------------- 路径
    def path_of(self, code: str) -> Path:
        return self.root / f"{resolve(code)}.parquet"

    def has(self, code: str) -> bool:
        p = self.path_of(code)
        return p.exists() and p.stat().st_size > 0

    def available(self) -> list[str]:
        """已落盘的 ETF 主名（``510300.SH`` 形式，按文件名排序）。"""
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.parquet") if p.stat().st_size > 0)

    # ---------------------------------------------------------------- 读取
    def load(self, code: str, start=None, end=None) -> pd.DataFrame:
        p = self.path_of(code)
        if not p.exists():
            raise EtfDataMissing(
                f"ETF {resolve(code)} 行情缺失：{p} 不存在。"
                f"请先运行 python scripts/fetch_etf.py"
            )
        df = pd.read_parquet(p)
        if df.empty:
            raise EtfDataMissing(f"ETF {resolve(code)} 行情为空文件：{p}")

        # 自证身份：防止把个股/指数 parquet 误放进本目录
        if "kind" in df.columns:
            kinds = set(df["kind"].dropna().unique().tolist())
            if kinds and kinds != {"etf"}:
                raise ValueError(
                    f"{p.name} 的 kind={kinds} 不是 etf —— 疑似个股/指数数据误放"
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

    def load_or_none(self, code: str, start=None, end=None) -> pd.DataFrame | None:
        """缺数据返回 ``None``（池子逐只遍历时用，避免一只缺失炸掉整轮）。"""
        try:
            return self.load(code, start, end)
        except (EtfDataMissing, ValueError):
            return None

    def close_series(self, code: str, start=None, end=None) -> pd.Series:
        df = self.load(code, start, end)
        return pd.Series(df["close"].values, index=df["time"].dt.date.values,
                         name=resolve(code))

    def daily_return(self, code: str, start=None, end=None) -> pd.Series:
        """**价格口径**日收益（不含分红）。做信号用；做收益归因请用 ``total_return``。"""
        return self.close_series(code, start, end).pct_change().dropna()

    # ---------------------------------------------------------------- 写入
    def save(self, code: str, df: pd.DataFrame,
             actions: "list[dict] | None" = None,
             action_store=None) -> Path:
        """落盘 ETF 日线。强制打 ``kind='etf'``、``etf_code``、``symbol``。

        ⚠️ 与 ``IndexBarStore.save`` 的差别：**保留 ``symbol`` 列** ——
        ETF 是投资标的不是基准，下游要靠它做分组与对账。

        同时附上份额折算复权列（``split_factor`` / ``adj_close`` / ``has_action``）：
        ``actions`` 显式传入则用之，否则从 ``action_store``（默认
        ``data_cache/etf_actions.parquet``）按 symbol 取。两者都没有 →
        因子恒为 1、``adj_close == close``，且 ``has_action=False``。
        """
        out = df.copy()
        if "time" not in out.columns:
            raise ValueError("ETF 日线必须有 time 列")
        out["time"] = pd.to_datetime(out["time"])
        code_full = resolve(code)
        out["symbol"] = code_full
        out["etf_code"] = code_full
        out["kind"] = "etf"
        # 价格口径自描述：本表的 ``close`` 是**不复权**的真实成交价（含折算跳空与分红除权）。
        # 不要在 close 上做复权 —— 复权基准会随最新价漂移，且复权价不能用于撮合。
        out["price_caliber"] = PRICE_CALIBER_RAW

        from aq.data.etf_actions import EtfActionStore, apply_restore

        if actions is None:
            store = action_store if action_store is not None else EtfActionStore()
            actions = store.load_or_none(code)
        out = apply_restore(out, actions)

        out = out.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)
        return _atomic_write(out, self.path_of(code))

    def save_many(self, frames: dict[str, pd.DataFrame]) -> int:
        ok = 0
        for code, df in frames.items():
            self.save(code, df)
            ok += 1
        return ok

    # ---------------------------------------------------------------- 面板
    def panel(self, codes: list[str], field: str = "close",
              start=None, end=None) -> pd.DataFrame:
        """宽表面板：index=日期，columns=``510300.SH`` 形式。

        逐只缺失的标的**直接跳过**（不报错），并在返回的 ``DataFrame.attrs``
        里列出被跳过的代码 —— 静默丢标的正是"看起来能跑、其实少了一半"的来源。
        """
        cols: dict[str, pd.Series] = {}
        skipped: list[str] = []
        for code in codes:
            df = self.load_or_none(code, start, end)
            if df is None or len(df) == 0 or field not in df.columns:
                skipped.append(resolve(code))
                continue
            s = pd.Series(pd.to_numeric(df[field], errors="coerce").values,
                          index=df["time"], name=resolve(code))
            cols[resolve(code)] = s
        out = pd.DataFrame(cols).sort_index()
        out.attrs["skipped"] = skipped
        out.attrs["universe_caliber"] = UNIVERSE_CALIBER
        out.attrs["price_caliber"] = PRICE_CALIBER_RAW
        return out

    # ---------------------------------------------------------------- 全收益
    def total_return(self, code: str, start=None, end=None,
                     nav_store: "EtfNavStore | None" = None,
                     dividend_store: "EtfDividendStore | None" = None) -> pd.Series:
        """**全收益**日收益序列。**优先走权威净值口径**。

        路径 1（首选）``EtfNavStore.total_return`` —— **官方日增长率**（全收益）。
        含分红、份额折算中性、无单位歧义。attrs ``price_caliber = NAV_CALIBER_TOTAL``。

        路径 2（退化）价格 + 分红自构造：``r_t = (A_t + ΔD_t·F_t) / A_{t−1} − 1``

        - ``A`` = ``adj_close`` = ``close × split_factor``
        - ``D`` = 累计每份现金分红，在**除息日**计入
        - ``F`` = 份额折算因子：分红按「每份」发放，份额翻倍后同样一份
          （虚拟份额）领到的现金也要按 ``F`` 放大，否则拆分后分红被低估一半

        ⚠️ 为什么退化路径不可靠（实测）
        - 新浪 ETF 分红接口对红利类普遍缺漏（512890 只报 1 条且为 0），
          东财 ``fund_fh_em`` 只覆盖当年 → **分红表覆盖不足**；
        - 东财「累计净值」的绝对水平对部分标的不可解释（588000 累计 < 单位），
          因此**不要**用 ``累计 − 单位`` 反推分红金额。
        → 只要净值在库，就必须走路径 1；路径 2 仅作交叉校验。

        ⚠️ 为什么不用行情源的复权价：实测腾讯 ``adjust=`` 对 ETF 三档互相矛盾
        （510050 默认 3.38x / qfq 37.4x / hfq 4.29x）。
        """
        ns = nav_store if nav_store is not None else EtfNavStore()
        if ns.has(code):
            return ns.total_return(code, start, end)

        ds = dividend_store or EtfDividendStore()
        px = self.load(code, start, end)
        idx = px["time"]
        close = pd.to_numeric(px["close"], errors="coerce")
        fac = (pd.to_numeric(px["split_factor"], errors="coerce").fillna(1.0)
               if "split_factor" in px.columns else pd.Series(1.0, index=px.index))
        a = pd.Series((close * fac).values, index=idx).dropna()

        dv = ds.load_or_none(code)
        if dv is None or dv.empty:
            s = a.pct_change().dropna()
            s.attrs["price_caliber"] = PRICE_CALIBER_SPLIT_ADJ
            s.attrs["dividend_missing"] = True
            s.attrs["universe_caliber"] = UNIVERSE_CALIBER
            return s
        d = pd.Series(pd.to_numeric(dv["cum_div"], errors="coerce").values,
                      index=pd.to_datetime(dv["time"]))
        d = d[~d.index.duplicated(keep="last")].reindex(a.index, method="ffill").fillna(0.0)
        f = pd.Series(fac.values, index=idx).reindex(a.index).ffill().fillna(1.0)
        cash = d.diff().fillna(0.0) * f          # 每虚拟份额领到的现金
        r = ((a + cash) / a.shift(1) - 1.0).dropna()
        r.attrs["price_caliber"] = PRICE_CALIBER_TOTAL
        r.attrs["universe_caliber"] = UNIVERSE_CALIBER
        return r

    def total_return_from_nav(self, code: str, nav: pd.DataFrame,
                              start=None, end=None) -> pd.Series:
        """**交叉校验用**的全收益：由净值表构造（官方日增长率）。

        与价格路径完全独立 —— 一个来自二级市场成交价，一个来自基金公司
        公布净值，两者应当一致。不一致说明：价格源有问题、分红缺失、
        或存在未识别的折算（后者就是 ``etf_actions`` 要解决的事）。

        ⚠️ 本方法**不做单位假设**：交给 ``etf_actions.nav_total_return``，
        它会在整列像百分数时自动 /100 并打印警告。早期版本写死 ``/100.0``
        再配 ``d < 5.0`` 过滤 —— 那个组合正是让 7/7 个折算事件全错的元凶。
        """
        from aq.data.etf_actions import nav_total_return

        r, src, unit = nav_total_return(nav)
        r = r[(r > -1.0) & (r < 5.0)]
        r.name = resolve(code)
        if start is not None:
            r = r[r.index >= pd.Timestamp(_as_ts(start))]
        if end is not None:
            r = r[r.index <= pd.Timestamp(_as_ts(end))]
        r.attrs["price_caliber"] = NAV_CALIBER_TOTAL
        r.attrs["ret_src"] = src
        r.attrs["ret_unit"] = unit
        r.attrs["universe_caliber"] = UNIVERSE_CALIBER
        return r

    def reapply_actions(self, code: str, action_store=None) -> Path:
        """把折算复权列**就地重算**到已落盘的表（不改 ``close``）。

        用途：日线先于折算事件落盘时（事件是后来才反解出来的），
        不必重新联网抓行情，只需重算因子列。
        """
        df = pd.read_parquet(self.path_of(code))
        df["time"] = pd.to_datetime(df["time"])
        from aq.data.etf_actions import EtfActionStore

        store = action_store if action_store is not None else EtfActionStore()
        return self.save(code, df, actions=store.load_or_none(code))


# ---------------------------------------------------------------------------
# 净值（权威全收益来源）
# ---------------------------------------------------------------------------

#: ``data_cache/etf_nav/*.parquet`` 的契约列。
#: ``nav_ret`` 一律是**小数**（东财原始「日增长率」是百分数，入库时 /100）。
NAV_COLS = ("symbol", "time", "unit_nav", "cum_nav", "nav_ret",
            "nav_caliber", "kind")


class EtfNavStore:
    """``data_cache/etf_nav/`` 的读写封装 —— **权威收益来源**。

    ⭐ 为什么把净值提到一等公民
    --------------------------
    ETF 的二级市场价格**不是**收益序列：它含分红除权跳空、含份额折算跳空
    （512890 因 1:2 拆分单日"跌" −51.13%）。而基金公司公布的净值里带一条
    **官方「日增长率」**，它就是全收益：

    - **非除息日它精确等于单位净值增长率**（实测 510880/510300/511010 残差
      恰为 0，其余 ≈2.5e-5 = 只保留 2 位小数的舍入）；
    - **除息日它比单位净值增长率多出分红那一块**；
    - 份额折算日它给的是**真实市场涨跌**（512890 拆分日 −2.16%，而同期
      单位净值裸跌 −51%、价格裸跌 −51%）。

    年化量级也对得上：515080 官方 11.28% vs 单位净值 7.14%（差 4.14pp ≈
    中证红利股息率）；不分红的 159915 两者几乎相同。
    证据：``scripts/probes/probe_etf_dividend_dilution.py``、``probe_etf_dividend_truth.py``。

    ❌ **不要用累计净值比率当全收益**（本项目一度这样做，已纠正）。
    标准定义 ``累计净值 = 单位净值 + 累计分红 D``，于是
    ``累计比 − 1 = r_true/(1 + D/U)`` —— 一个被累计分红**稀释**的版本。
    实测 510880 有 56.8% 交易日 ``|累计比 − 官方日增长率| > 5e-4``（max 0.0199），
    而不分红的 512890 偏差恰为 0 → 偏差只在分红标的上出现，正是稀释特征。
    另外累计净值在折算日还可能被重述（512890 拆分后 累计 = 2×单位）。

    ⚠️ 官方「日增长率」是**百分数**，入库时已 /100 存小数（``coerce_return_unit``
    守卫，见 ``etf_actions``）。把百分数当小数用会静默错 100 倍。
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else default_nav_dir()
        if not self.root.is_absolute():
            self.root = PROJECT_ROOT / self.root

    def path_of(self, code: str) -> Path:
        return self.root / f"{resolve(code)}.parquet"

    def has(self, code: str) -> bool:
        p = self.path_of(code)
        return p.exists() and p.stat().st_size > 0

    def available(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.parquet")
                      if p.stat().st_size > 0)

    def load(self, code: str, start=None, end=None) -> pd.DataFrame:
        p = self.path_of(code)
        if not p.exists():
            raise EtfDataMissing(
                f"ETF {resolve(code)} 净值缺失：{p} 不存在。"
                f"请先运行 python scripts/fetch_etf.py --nav"
            )
        df = pd.read_parquet(p)
        if df.empty:
            raise EtfDataMissing(f"ETF {resolve(code)} 净值为空文件：{p}")
        df["time"] = pd.to_datetime(df["time"])
        if "kind" in df.columns:
            kinds = set(df["kind"].dropna().unique().tolist())
            if kinds and kinds != {"etf"}:
                raise ValueError(f"{p.name} 的 kind={kinds} 不是 etf")
        if start is not None:
            df = df[df["time"] >= pd.Timestamp(_as_ts(start))]
        if end is not None:
            df = df[df["time"] <= pd.Timestamp(_as_ts(end))]
        return df.sort_values("time").reset_index(drop=True)

    def load_or_none(self, code: str, start=None, end=None) -> pd.DataFrame | None:
        try:
            return self.load(code, start, end)
        except (EtfDataMissing, ValueError):
            return None

    # ---------------------------------------------------------------- 写入
    def save(self, code: str, df: pd.DataFrame) -> Path:
        """落盘净值。强制 ``kind='etf'``、``symbol``、``nav_caliber``。

        **必需列：``time`` 与 ``nav_ret``**（官方日增长率，全收益的唯一来源）。
        ``cum_nav`` / ``unit_nav`` 缺失也能存，但会失去诊断与隐含分红能力。

        ``nav_ret`` 若疑似百分数会被 ``etf_actions.coerce_return_unit`` 纠正
        并打印警告 —— 这道守卫是必需的：把百分数当小数用不会报错，
        只会**静默**给出错 100 倍的收益（本项目已踩过一次，7/7 事件全错）。
        """
        from aq.data.etf_actions import coerce_return_unit

        out = df.copy()
        if "time" not in out.columns:
            raise ValueError("ETF 净值必须有 time 列")
        if "nav_ret" not in out.columns:
            raise ValueError(
                "ETF 净值必须有 nav_ret 列（官方日增长率 = 全收益的唯一来源）"
            )
        out["time"] = pd.to_datetime(out["time"])
        for c in ("unit_nav", "cum_nav"):
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")
        # 先去重排序，再判单位 —— 锚（单位净值比率）必须在有序序列上算
        out = (out.sort_values("time").drop_duplicates(subset=["time"], keep="last")
               .reset_index(drop=True))
        anchor = (out["unit_nav"].pct_change()
                  if "unit_nav" in out.columns else None)
        fixed, _unit = coerce_return_unit(out["nav_ret"], anchor=anchor)
        out["nav_ret"] = pd.to_numeric(fixed, errors="coerce")
        out["symbol"] = resolve(code)
        out["kind"] = "etf"
        out["nav_caliber"] = NAV_CALIBER_TOTAL
        return _atomic_write(out, self.path_of(code))

    # ---------------------------------------------------------------- 收益
    def total_return(self, code: str, start=None, end=None) -> pd.Series:
        """**权威全收益**日收益（小数）= 官方日增长率。

        attrs：``price_caliber = NAV_CALIBER_TOTAL``、``ret_src = nav_ret``、
        ``ret_unit``（是否发生过百分数→小数纠正）、
        ``universe_caliber = live_only_biased``（绝对收益仍不可引用，见模块 docstring）。
        """
        from aq.data.etf_actions import nav_total_return

        n = self.load(code, start, end)
        r, src, unit = nav_total_return(n)
        r.name = resolve(code)
        r.attrs["price_caliber"] = (
            NAV_CALIBER_TOTAL if src == "nav_ret" else NAV_CALIBER_CUM_RATIO_DILUTED
        )
        r.attrs["ret_src"] = src
        r.attrs["ret_unit"] = unit
        r.attrs["universe_caliber"] = UNIVERSE_CALIBER
        return r

    def return_panel(self, codes: list[str], start=None, end=None) -> pd.DataFrame:
        """宽表全收益面板（index=日期，columns=``510300.SH``）。

        逐只缺失**跳过**并在 ``attrs['skipped']`` 记录 —— 静默丢标的正是
        "看起来能跑、其实少了一半"的典型来源。
        """
        cols: dict[str, pd.Series] = {}
        skipped: list[str] = []
        for code in codes:
            try:
                s = self.total_return(code, start, end)
            except (EtfDataMissing, ValueError):
                skipped.append(resolve(code))
                continue
            if len(s) < 30:
                skipped.append(resolve(code))
                continue
            cols[resolve(code)] = s
        out = pd.DataFrame(cols).sort_index()
        out.attrs["skipped"] = skipped
        out.attrs["price_caliber"] = NAV_CALIBER_TOTAL
        out.attrs["universe_caliber"] = UNIVERSE_CALIBER
        return out

    def verify(self, code: str, tol: float = 1e-4) -> dict:
        """**入库不变量**：官方日增长率是否真的在跟踪净值。返回的 ``ok`` 只看这一条。

        不变量（一行）
        --------------
        由「官方日增长率 = 全收益」的恒等式，反解每份分红
        ``d_t = U_{t−1}·(1 + r_t) − U_t``，两边同除 ``U_{t−1}`` 得

            ``d_t / U_{t−1} = r_t − u_t``，其中 ``u_t`` = 单位净值比率。

        于是 **``|r_t − u_t|`` 就是"每份分红占前收的比例"**。它的**中位数**必须小：
        分红一年只有 1~12 天，中位数对它们是稳健的；而单位口径错误会让
        **所有**日期一起偏 100 倍 ⇒ 中位数必然爆表。实测 8 只核心标的
        ``dev_med`` = 2.5e-05~3.1e-05，**正好等于**官方列"2 位小数百分比"的
        舍入步长（0.01% / 2）—— 这就是源数据的精度天花板。

        ❌ **踩过的两个错判据（都不要再用）**

        1. **「``|累计比 − nav_ret|`` 要小」**：对分红标的它**本来就应该大**
           （累计比是被累计分红稀释的版本），会把全部红利 ETF 误报成脏数据。
        2. **「只在 ``|累计比 − 单位比| < 1e-9`` 的那些天比较」**：看着很讲道理，
           实际是**退化样本** —— 两个比率都由 4 位小数的净值列算出，要逐位相等
           几乎只在"当日净值完全没动、两个比率都是 0"时成立。于是判据只剩
           一堆 ``0 vs 0``，对 510300 / 511010 甚至**全部为 0**，灵敏度归零
           （由 ``probe_etf_guard_roundtrip.py`` 第 3 段负对照实测抓出）。

        ⚠️ 质量判据**不能用 ``max``**：会被两类与口径无关的日子占满 ——
        ①**同步重述日**（净值归一/份额折算让单位与累计一起跳，511880 在
        2013-04-03 由 1.0000 归一为 100.074 ⇒ ``|r−u|`` 达 99.07）；
        ②**源数据单日孤点**（510050 在 2005-02-04 单位净值 −10.55% 而官方列
        写 +5.89%，基金成立初期噪声）。中位数天然免疫这两者。

        返回值：``ok``（唯一判据）+ ``dev_med/dev_p99/dev_max``（供人工看）
        + ``n_div_days``（分红日计数）+ ``n_restate_days``（同步重述日计数）
        + ``min_implied_d``（隐含每份分红的**绝对值**下限，用于发现
        单位净值自身的未记录跳变）。
        """
        from aq.data.etf_actions import NAV_RESTATE_ABS_RATIO, nav_total_return

        n = self.load(code)
        ret, src, unit = nav_total_return(n)
        out: dict = {"symbol": resolve(code), "ret_src": src, "ret_unit": unit,
                     "n": int(len(ret)), "ok": True}
        if "unit_nav" not in n.columns:
            return out
        un = pd.Series(pd.to_numeric(n["unit_nav"], errors="coerce").values,
                       index=n["time"]).dropna()
        u_ratio = un.pct_change()
        both = pd.DataFrame({"u": u_ratio, "r": ret}).dropna()
        if len(both) >= 30:
            dev = (both["r"] - both["u"]).abs()
            out["n_compare_days"] = int(len(dev))
            out["dev_med"] = float(dev.median())
            out["dev_p99"] = float(dev.quantile(0.99))
            out["dev_max"] = float(dev.max())
            # 分红日：隐含分红超过前收的 0.1%（日频分红远小于这个量级）
            out["n_div_days"] = int((dev > 1e-3).sum())
            out["ok"] = bool(out["dev_med"] <= tol)
        # 同步重述日（净值归一/份额折算）：单位净值自身跳变的那些天
        out["n_restate_days"] = int((u_ratio.abs() >= NAV_RESTATE_ABS_RATIO).sum())
        d = (un.shift(1) * (1.0 + ret.reindex(un.index)) - un)
        out["min_implied_d"] = float(d.min())
        out["abs_implied_d_med"] = float(d.abs().median())
        out["n_negative_d"] = int((d < -1e-6).sum())
        return out

    def dilution_residual(self, code: str) -> pd.DataFrame:
        """**口径诊断**：累计净值比率相对官方日增长率的偏差结构。

        返回三列 ``ret(官方) / cum_ratio(累计比) / resid``。

        预期形态（这是"累计比 ≠ 全收益"的直接证据）：

        - 不分红标的（512890/159915）：``resid`` 全期 ≈ 0（<2e-4）；
        - 分红标的（510880/515080/510300）：``resid`` 与 ``|ret|`` 同量级
          正相关，比例 ≈ ``(D/U)/(1+D/U)``，且**只在分红标的上出现**。

        ⚠️ 若某只标的 ``resid`` 大到无法用稀释解释（例如 ``cum_ratio`` 与
        ``ret`` 反号），说明该标的净值数据自相矛盾 → 不要入库使用。
        """
        from aq.data.etf_actions import nav_total_return

        n = self.load(code)
        if "cum_nav" not in n.columns:
            raise ValueError(f"{code}: 无 cum_nav 列，无法做稀释诊断")
        ret, _src, _unit = nav_total_return(n)
        cum = pd.Series(pd.to_numeric(n["cum_nav"], errors="coerce").values,
                        index=n["time"]).dropna()
        cum_ratio = cum.pct_change()
        out = pd.DataFrame({"ret": ret, "cum_ratio": cum_ratio})
        out["resid"] = (out["cum_ratio"] - out["ret"]).abs()
        return out.dropna()

    def implied_dividend(self, code: str) -> pd.Series:
        """**隐含每份分红**（元/份）：``d_t = U_{t−1}·(1 + r_t) − U_t``（下截 0）。

        由「官方日增长率是全收益」这一恒等式反解得到，**不需要分红表** ——
        实测新浪 ETF 分红接口对红利类普遍缺漏（512890 只报 1 条且为 0）、
        东财 ``fund_fh_em`` 只覆盖当年，两张表都不能当历史分红源。

        ⚠️ 只能在**单位净值未被折算重述**的日子上读；折算日会给出假的巨额分红。
        因此本函数把结果交给调用方自行甄别（正值为分红，异常大值为折算）。
        """
        n = self.load(code)
        if "unit_nav" not in n.columns:
            raise ValueError(f"{code}: 无 unit_nav 列，无法反解分红")
        from aq.data.etf_actions import nav_total_return

        ret, _src, _unit = nav_total_return(n)
        u = pd.Series(pd.to_numeric(n["unit_nav"], errors="coerce").values,
                      index=n["time"]).dropna()
        d = (u.shift(1) * (1.0 + ret.reindex(u.index)) - u).clip(lower=0.0)
        return d.dropna()


# ---------------------------------------------------------------------------
# 分红
# ---------------------------------------------------------------------------

class EtfDividendStore:
    """``data_cache/etf_dividends.parquet`` 的读写封装（长表）。

    长表列：``symbol / time / cum_div``。``cum_div`` = **累计**每份派现（元），
    来自新浪 ``fund_etf_dividend_sina(symbol=...)``（逐只接口）。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_dividend_path()
        if not self.path.is_absolute():
            self.path = PROJECT_ROOT / self.path

    def load(self) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame(columns=["symbol", "time", "cum_div"])
        df = pd.read_parquet(self.path)
        df["time"] = pd.to_datetime(df["time"])
        return df.sort_values(["symbol", "time"]).reset_index(drop=True)

    def load_or_none(self, code: str) -> pd.DataFrame | None:
        df = self.load()
        if df.empty:
            return None
        sub = df[df["symbol"] == resolve(code)]
        return sub.reset_index(drop=True) if len(sub) else None

    def save_rows(self, code: str, dv: pd.DataFrame) -> None:
        """把单只的分红记录并入长表（幂等：同 symbol 全量替换）。"""
        if dv is None or len(dv) == 0:
            return
        out = dv.copy()
        out["time"] = pd.to_datetime(out["time"])
        out["symbol"] = resolve(code)
        out = out[["symbol", "time", "cum_div"]]
        cur = self.load()
        cur = cur[cur["symbol"] != resolve(code)] if not cur.empty else cur
        merged = pd.concat([cur, out], ignore_index=True) if not cur.empty else out
        merged = (merged.sort_values(["symbol", "time"])
                  .drop_duplicates(subset=["symbol", "time"], keep="last")
                  .reset_index(drop=True))
        _atomic_write(merged, self.path)

    def coverage(self) -> dict[str, int]:
        df = self.load()
        if df.empty:
            return {}
        return df.groupby("symbol").size().to_dict()


# ---------------------------------------------------------------------------
# 清单（point-in-time 的**能拿到的那部分**）
# ---------------------------------------------------------------------------

#: ``data_cache/etf_list.parquet`` 的契约列。
ETF_LIST_COLS = (
    "symbol",        # 510300.SH 形式
    "code",          # 510300
    "name",          # 中文简称
    "market",        # SH / SZ
    "list_date",     # 上市日（= 日线首日，无偏）
    "live_from",     # 本清单认为它"存在"的起点（= list_date）
    "last_bar_date", # 已落盘日线的最后一天（< 样本末 = 疑似已停止交易）
    "n_bars",        # 已落盘日线行数
    "source",        # 成员关系来源：em_spot / sina_list / both
    "updated_at",
)


def load_etf_list(path: str | Path | None = None) -> pd.DataFrame:
    """读 ETF 清单。缺文件抛 ``EtfDataMissing``（**不返回空表**）。

    ⚠️ 返回的清单只含**现役** ETF —— 已清盘/已退市的拿不到，
    因此 ``universe_caliber`` 恒为 ``live_only_biased``。
    完整偏差说明见 ``scripts/probes/probe_etf_survivorship.py``。
    """
    p = Path(path) if path else default_list_path()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        raise EtfDataMissing(
            f"ETF 清单缺失：{p} 不存在。请先运行 python scripts/fetch_etf.py --list"
        )
    df = pd.read_parquet(p)
    if df.empty:
        raise EtfDataMissing(f"ETF 清单为空文件：{p}")
    if "list_date" in df.columns:
        df["list_date"] = pd.to_datetime(df["list_date"])
    return df.sort_values("symbol").reset_index(drop=True)


def save_etf_list(df: pd.DataFrame, path: str | Path | None = None) -> Path:
    p = Path(path) if path else default_list_path()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    out = df.copy()
    for c in ("list_date", "live_from", "last_bar_date", "updated_at"):
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce")
    return _atomic_write(out.sort_values("symbol").reset_index(drop=True), p)


def eligible_at(df_list: pd.DataFrame, asof, min_list_days: int = 180,
                min_amount_by_symbol: dict[str, float] | None = None,
                min_amount: float = 0.0) -> list[str]:
    """某一天**可交易**的 ETF 池（point-in-time 过滤）。

    - 上市满 ``min_list_days`` 个自然日；
    - 若给 ``min_amount_by_symbol``，再按成交额门槛过滤（**用 asof 之前的
      滚动均值**，调用方负责保证那个均值不含未来数据）。

    ⚠️ 这只做到"上市日之前不入选"这一半的 PIT ——
    "已退市之后不该入选"那一半**做不到**（拿不到退市清单）。
    """
    ts = pd.Timestamp(_as_ts(asof))
    if df_list.empty:
        return []
    m = df_list["list_date"] <= (ts - pd.Timedelta(days=min_list_days))
    if min_amount_by_symbol and min_amount > 0:
        m &= df_list["symbol"].map(
            lambda s: min_amount_by_symbol.get(str(s), 0.0) >= min_amount
        )
    return sorted(df_list.loc[m, "symbol"].astype(str).tolist())
