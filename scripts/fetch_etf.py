"""ETF 数据抓取：清单 + 日线 + 分红。

为什么单独一个脚本
------------------
ETF 与个股虽然都是"标的"，但三条口径都不一样，塞进 ``fetch_daily.py`` 会
把差异藏起来：

====================  ==========================  ====================
维度                  个股                         ETF
====================  ==========================  ====================
复权                  后复权 + 不复权双序列        **只能不复权**（见下）
分红                  含在复权因子里              必须单独存（拖累 1.7~3.2pp/年）
成员关系              代码段可识别                **只能靠来源清单**
退市数据              腾讯源可拿                  **拿不到** → 单向偏差
====================  ==========================  ====================

为什么 ETF 不做复权
-------------------
实测 ``stock_zh_a_hist_tx`` 对 ETF 忽略 ``adjust`` 参数（三档 close 完全相同），
而东财「累计净值」**含份额折算**（510300 单位净值 3.03 vs 累计净值 1.22）。
两源交叉验证：腾讯与新浪在首日/末日逐位一致
（510050：2005-02-23 双双 0.876，2026-09-15 双双 2.958）
→ 都是**真实成交价**。所以：

- 撮合与估值只用**不复权真实价**（``price_caliber="raw_no_dividend"``）；
- 收益归因走 ``EtfBarStore.total_return()``（价格 + 分红自构造）；
- **禁止**在表里做复权 —— 复权基准随最新价漂移，且复权价无法撮合。

数据源
------
- 清单：``fund_etf_spot_em``（东财，含成交额/换手/流通市值）
        + ``fund_etf_category_sina``（新浪，可补东财遗漏）
- 日线：``stock_zh_a_hist_tx``（腾讯，主，2005-02-23 起，含 amount）
        备：``fund_etf_hist_sina``（新浪，通常比腾讯新 2 个交易日）
- 分红：``fund_etf_dividend_sina(symbol=...)``（**逐只**接口）
        备/校验：``fund_fh_em``（东财基金分红，全量表）
- 净值：``fund_etf_fund_info_em``（仅用于交叉校验，**不当复权因子**）

用法::

    python scripts/fetch_etf.py --list                 # 建清单骨架
    python scripts/fetch_etf.py --group core           # 拉核心子集（宽基+红利+债+商品+跨境）
    python scripts/fetch_etf.py --all --workers 6      # 拉全部现役 ETF
    python scripts/fetch_etf.py --list --refresh-dates # 从已落盘日线回填 list_date
    python scripts/fetch_etf.py --dividends --group core
    python scripts/fetch_etf.py --verify
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from aq.config.settings import PROJECT_ROOT  # noqa: E402
from aq.data.etf_actions import EtfActionStore  # noqa: E402
from aq.data.etf_store import (  # noqa: E402
    ETF_LIST_COLS,
    UNIVERSE_CALIBER,
    EtfBarStore,
    EtfDividendStore,
    EtfNavStore,
    default_list_path,
    load_etf_list,
    market_of,
    resolve,
    save_etf_list,
)

_print_lock = threading.Lock()

#: 默认抓取区间。2005-02-23 = 510050 上市首日（最早 ETF）。
DEFAULT_START = "2004-01-01"
DEFAULT_END = "2026-09-15"


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# 分组（先小后大，管线验证用）
# ---------------------------------------------------------------------------

#: ``--group`` 可选篮子。**故意手工指定而非按名称正则** ——
#: 名称匹配会把"红利"类漏掉一半（如 515180 叫"红利ETF易方达"、
#: 563020 叫"低波红利ETF"），而这几只正是 B 路线的核心。
GROUPS: dict[str, list[str]] = {
    # 宽基 6 + 红利 5 + 债 3 + 商品 1 + 跨境 3 = 18 只，覆盖全部资产类别
    "core": [
        "510050",  # 上证50ETF（2005-02-23，最早）
        "510300",  # 沪深300ETF
        "510500",  # 中证500ETF
        "512100",  # 中证1000ETF
        "159915",  # 创业板ETF
        "588000",  # 科创50ETF
        "510880",  # 上证红利ETF（2007-01-18）
        "512890",  # 红利低波ETF ← B 路线核心标的
        "515180",  # 红利ETF易方达
        "563020",  # 低波红利ETF
        "515080",  # 中证红利ETF
        "511010",  # 国债ETF
        "511260",  # 十年国债ETF
        "511880",  # 银华日利（货币）
        "518880",  # 黄金ETF
        "513100",  # 纳指ETF
        "513500",  # 标普500ETF
        "159920",  # 恒生ETF
    ],
    # 宽基为主，验证"跨市场/跨资产"的代码前缀解析
    "broad": ["510050", "510300", "510500", "512100", "159915", "588000", "159901"],
    "dividend": ["510880", "512890", "515180", "563020", "515080", "512990"],
}


def _ak():
    import akshare as ak

    return ak


# ---------------------------------------------------------------------------
# 日线抓取
# ---------------------------------------------------------------------------

_TX_RENAME = {"date": "time", "turnover": "turnover"}
_SINA_RENAME = {"date": "time", "prevclose": "prevclose"}


def _norm_bars(df: pd.DataFrame, src: str) -> pd.DataFrame:
    """把腾讯/新浪的列名统一成 time/open/high/low/close/volume/amount。"""
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    rename = {"日期": "time", "date": "time"}
    if src == "tx":
        rename.update({"turnover": "turnover_rate"})
    out = out.rename(columns={k: v for k, v in rename.items() if k in out.columns})
    if "time" not in out.columns:
        raise ValueError(f"无法识别日期列：{list(out.columns)}")
    out["time"] = pd.to_datetime(out["time"])
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        elif c in ("volume", "amount"):
            out[c] = pd.NA
        else:
            raise ValueError(f"缺列 {c}，实际列：{list(out.columns)}")
    keep = [c for c in ("time", "open", "high", "low", "close", "volume",
                        "amount", "turnover_rate") if c in out.columns]
    return (out[keep].dropna(subset=["close"])
            .sort_values("time").drop_duplicates(subset=["time"])
            .reset_index(drop=True))


def fetch_bars_tx(code: str, start: str, end: str) -> pd.DataFrame:
    ak = _ak()
    sym = f"{market_of(code).lower()}{code}"
    raw = ak.stock_zh_a_hist_tx(symbol=sym, start_date=start.replace("-", ""),
                                end_date=end.replace("-", ""))
    if raw is None or len(raw) == 0:
        raise ValueError("腾讯源返回空")
    return _norm_bars(raw, "tx")


def fetch_bars_sina(code: str, start: str, end: str) -> pd.DataFrame:
    ak = _ak()
    sym = f"{market_of(code).lower()}{code}"
    raw = ak.fund_etf_hist_sina(symbol=sym)
    if raw is None or len(raw) == 0:
        raise ValueError("新浪源返回空")
    df = _norm_bars(raw, "sina")
    df = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] <= pd.Timestamp(end))]
    if df.empty:
        raise ValueError("新浪源在区间内为空")
    return df.reset_index(drop=True)


def fetch_one_bars(code: str, start: str, end: str, tries: int = 3,
                   fallback: bool = True) -> tuple[str, pd.DataFrame | None, str]:
    """返回 ``(状态, 数据, 备注)``，状态 ∈ ok / fail。腾讯主演、新浪备。"""
    last = ""
    for attempt in range(1, tries + 1):
        try:
            return "ok", fetch_bars_tx(code, start, end), "tx"
        except Exception as exc:  # noqa: BLE001
            last = f"tx:{type(exc).__name__}"
            if attempt < tries:
                time.sleep(1.2 * attempt)
    if fallback:
        for attempt in range(1, tries + 1):
            try:
                return "ok", fetch_bars_sina(code, start, end), "sina(fallback)"
            except Exception as exc:  # noqa: BLE001
                last += f" sina:{type(exc).__name__}"
                if attempt < tries:
                    time.sleep(1.2 * attempt)
    return "fail", None, last


def crosscheck_bars(code: str, start: str, end: str) -> dict:
    """同窗口逐日比腾讯 vs 新浪收盘价。返回偏差统计。

    ⚠️ 只用于**抽样**（每只两次网络请求）。全量跑会把抓取时间翻倍，
    而两源在首日/末日的逐位一致已被证实，无需逐只复核。
    """
    tx = fetch_bars_tx(code, start, end)
    sn = fetch_bars_sina(code, start, end)
    a = tx.set_index("time")["close"].astype(float)
    b = sn.set_index("time")["close"].astype(float)
    j = pd.concat({"tx": a, "sina": b}, axis=1).dropna()
    if j.empty:
        return {"code": code, "n_overlap": 0, "max_rel": float("nan"),
                "first_same": False, "last_same": False}
    rel = (j["tx"] - j["sina"]).abs() / j["sina"].abs()
    return {
        "code": code,
        "n_overlap": int(len(j)),
        "max_rel": float(rel.max()),
        "mean_rel": float(rel.mean()),
        # 首末逐位一致是本项目判定"两源同口径"的既有判据
        "first_same": bool(abs(float(j["tx"].iloc[0]) - float(j["sina"].iloc[0])) < 1e-9),
        "last_same": bool(abs(float(j["tx"].iloc[-1]) - float(j["sina"].iloc[-1])) < 1e-9),
        "first": str(j.index[0].date()),
        "last": str(j.index[-1].date()),
    }


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------

#: 用「名称必须含 ETF」+「必须有场内日线」双重条件确认是 ETF。
#: 这两条缺一不可：``510080``（长盛全债指数增强，2004 年成立）名称不含 ETF、
#: 且新浪/腾讯都拿不到场内日线 —— 但它落在 510 代码段里，**光看代码会误收**。
ETF_NAME_TOKEN = "ETF"


def _live_sources() -> tuple[pd.DataFrame, pd.DataFrame]:
    """拉现役清单的两个来源。任一失败不阻塞另一个。"""
    ak = _ak()
    em = pd.DataFrame()
    sn = pd.DataFrame()
    try:
        em = ak.fund_etf_spot_em()
        print(f"  东财现役清单 fund_etf_spot_em：{len(em)} 行")
    except Exception as exc:  # noqa: BLE001
        print(f"  [警告] 东财现役清单失败：{type(exc).__name__}: {exc}")
    try:
        sn = ak.fund_etf_category_sina(symbol="ETF基金")
        print(f"  新浪现役清单 fund_etf_category_sina：{len(sn)} 行")
    except Exception as exc:  # noqa: BLE001
        print(f"  [警告] 新浪现役清单失败：{type(exc).__name__}: {exc}")
    return em, sn


def _codes_of(df: pd.DataFrame, col_hints: tuple[str, ...]) -> set[str]:
    if df is None or df.empty:
        return set()
    col = next((c for c in col_hints if c in df.columns), None)
    if col is None:
        return set()
    return set(df[col].astype(str).str.replace(r"^(sh|sz)", "", regex=True)
               .str.replace(r"\.(SH|SZ)$", "", regex=True).str.zfill(6))


def _name_map(df: pd.DataFrame) -> dict[str, str]:
    """按**行位置**配对「代码 → 名称」。

    ⚠️ 2026-09-18 修 P0 缺陷：旧代码写成
    ``dict(zip(_codes_of(df, ("代码",)), df["名称"]))`` ——
    ``_codes_of`` 返回的是 **set**（无序、去重），拿它去 zip 一列**有序**的名称，
    等于把名称按哈希顺序乱配到代码上。实测后果：

    - 510300 被配成「云计算50ETF新华」（真实＝华泰柏瑞沪深300ETF）
    - 510880 被配成「港股汽车ETF国泰」（真实＝华泰柏瑞红利ETF）
    - 512890 被配成「银行ETF华泰柏瑞」（真实＝华泰柏瑞红利低波ETF）

    凡按名称给 ETF 分类的东西（红利/低波/宽基分组、债/货/商品/跨境剔除）
    全部静默错位，且**不会报错** —— 只会给出一个看起来很正常的错答案。
    """
    if df is None or df.empty:
        return {}
    col = next((c for c in ("代码",) if c in df.columns), None)
    if col is None or "名称" not in df.columns:
        return {}
    codes = (df[col].astype(str)
             .str.replace(r"^(sh|sz)", "", regex=True)
             .str.replace(r"\.(SH|SZ)$", "", regex=True)
             .str.zfill(6))
    out: dict[str, str] = {}
    for c, n in zip(codes, df["名称"].astype(str)):
        out.setdefault(c, n)
    return out


# 事实锚：这几个代码叫什么名字是**公开事实**，不依赖任何数据源。
# 用它当硬校验 —— 名称错位是静默错误，只能靠外部事实抓。
NAME_ANCHORS: dict[str, tuple[str, ...]] = {
    "510300": ("沪深300", "沪深 300"),   # 华泰柏瑞沪深300ETF，2012-05 上市
    "510050": ("上证50", "50ETF"),       # 华夏上证50ETF，2004-12 上市
    "510880": ("红利",),                 # 华泰柏瑞红利ETF，2006-11 上市
    "518880": ("黄金",),                 # 华安黄金ETF，2013-07 上市
    "588000": ("科创50", "科创 50"),     # 华夏科创50ETF，2020-09 上市
    "159915": ("创业板",),               # 易方达创业板ETF，2011-09 上市
}


def check_name_anchors(names: dict[str, str]) -> list[str]:
    """用公开事实校验名称映射；返回不通过的锚点描述（空列表 = 全过）。"""
    bad = []
    for code, toks in NAME_ANCHORS.items():
        nm = names.get(code, "")
        if not any(t in nm for t in toks):
            bad.append(f"{code} 应为 {'/'.join(toks)}，实得 '{nm}'")
    return bad


def build_list(refresh_dates: bool = False, crosscheck_nav: int = 0) -> pd.DataFrame:
    """构造 ``data_cache/etf_list.parquet``。

    自检三项（对应计划里 task 22 的要求）：
    1. **两源差集逐条解释** —— 东财独有 / 新浪独有必须列出来，不能静默合并；
    2. **list_date 与净值首日交叉校验** —— 抽样 ``--nav-check N`` 只标的，
       比较「日线首日」与「净值首日」，差异 > 30 天就报出来；
    3. **早期标的是不是真 ETF** —— 对 2010 前上市的标的，强制要求
       「名称含 ETF」+「能拿到场内日线」，防止 ``510080`` 那类场外基金混入。

    ⚠️ ``list_date`` 取自**日线首日**（无偏：来自真实成交记录），
    因此需要先有日线。没有日线的代码留 ``NaT`` 并在报告里点名 ——
    **不猜、不填**。
    """
    store = EtfBarStore()
    em, sn = _live_sources()

    # ⚠️ 必须按行位置配对（_name_map），不能拿 set 去 zip 有序列 —— 详见 _name_map docstring
    names: dict[str, str] = {}
    names.update(_name_map(em))
    for c, n in _name_map(sn).items():
        names.setdefault(c, n)

    # --- 自检 0（硬）：名称映射的事实锚 ---
    # 名称错位不会报错、只会给错答案，所以必须在落盘前用公开事实挡住。
    bad_anchor = check_name_anchors(names)
    if bad_anchor:
        print("  ❌ 自检0：名称映射未通过事实锚，清单**拒绝落盘**：")
        for b in bad_anchor:
            print(f"      {b}")
        print("      → 典型原因：又拿 set 去 zip 有序列，或数据源改了列名。")
        raise SystemExit("名称映射自检失败（NAME_ANCHORS）")

    em_codes = _codes_of(em, ("代码",))
    sn_codes = _codes_of(sn, ("代码",))
    both = em_codes & sn_codes
    only_em = sorted(em_codes - sn_codes)
    only_sn = sorted(sn_codes - em_codes)
    all_codes = sorted(em_codes | sn_codes)

    print(f"  东财 {len(em_codes)} | 新浪 {len(sn_codes)} | 交集 {len(both)} | 并集 {len(all_codes)}")
    print(f"  >>> 差集：东财独有 {len(only_em)}，新浪独有 {len(only_sn)}")
    if only_em:
        print("      东财独有（前 20）：" + " ".join(only_em[:20]))
    if only_sn:
        print("      新浪独有（前 20）：" + " ".join(only_sn[:20]))
    print("      解释：两源口径不同（新浪只收 ETF 基金，东财含 LOF/分级残部），")
    print("            差集**不丢弃** —— 并集保留，来源记为 em_only / sina_only。")

    rows: list[dict] = []
    no_bars: list[str] = []
    for code in all_codes:
        symbol = resolve(code)
        name = names.get(code, "")
        src = ("both" if code in both else ("em_only" if code in em_codes else "sina_only"))
        if store.has(code):
            df = store.load(code)
            list_date = df["time"].iloc[0]
            last_bar = df["time"].iloc[-1]
            n_bars = len(df)
        else:
            list_date, last_bar, n_bars = pd.NaT, pd.NaT, 0
            no_bars.append(code)
        rows.append({
            "symbol": symbol, "code": code, "name": name,
            "market": market_of(code),
            "list_date": list_date,
            "live_from": list_date,
            "last_bar_date": last_bar,
            "n_bars": n_bars,
            "source": src,
            "updated_at": pd.Timestamp.now(),
        })
    out = pd.DataFrame(rows)

    # --- 自检 3：早期标的必须是真 ETF ---
    early = out[(out["n_bars"] > 0)
                & (out["list_date"] <= pd.Timestamp("2010-01-01"))].copy()
    bad = early[~early["name"].str.upper().str.contains(ETF_NAME_TOKEN, na=False)]
    if len(bad):
        print(f"  ⚠️ 自检3：{len(bad)} 只早期标名称不含 ETF，疑似场外基金混入：")
        for _, r in bad.head(20).iterrows():
            print(f"      {r['code']} {r['name']} list={str(r['list_date'])[:10]}")
    else:
        print(f"  ✅ 自检3：{len(early)} 只 2010 前上市标的全部名称含 ETF 且有场内日线")

    if no_bars:
        print(f"  ⚠️ {len(no_bars)} 只尚无日线 → list_date 留空（**不猜**）："
              + " ".join(no_bars[:20]) + (" ..." if len(no_bars) > 20 else ""))

    # --- 自检 2：list_date vs 净值首日 ---
    if crosscheck_nav > 0:
        _nav_crosscheck(out, crosscheck_nav)

    save_etf_list(out)
    print(f"  清单落盘：{default_list_path()}（{len(out)} 行，"
          f"有日线 {int((out['n_bars'] > 0).sum())}，无日线 {len(no_bars)}）")
    print(f"  ⚠️ universe_caliber = {UNIVERSE_CALIBER}（只含现役，单向偏差）")
    return out


def _nav_crosscheck(lst: pd.DataFrame, n: int) -> None:
    """抽样比较「日线首日」与「东财净值首日」。差异 > 30 天即报。"""
    ak = _ak()
    pool = lst[lst["n_bars"] > 0].sort_values("list_date").head(n)
    print(f"  --- 自检2：日线首日 vs 净值首日（抽 {len(pool)} 只，早→晚）---")
    for _, r in pool.iterrows():
        code = r["code"]
        try:
            nav = ak.fund_etf_fund_info_em(fund=code, start_date="20000101",
                                           end_date="20260915")
            nav_first = pd.to_datetime(nav["净值日期"].iloc[0])
        except Exception as exc:  # noqa: BLE001
            print(f"      {code} 净值不可得（{type(exc).__name__}）")
            continue
        gap = (pd.Timestamp(r["list_date"]) - nav_first).days
        flag = "  ⚠️ 差 >30 天" if abs(gap) > 30 else ""
        print(f"      {code} {str(r['name'])[:14]:<14} 日线 {str(r['list_date'])[:10]} "
              f"净值 {nav_first.date()} 差 {gap:>5} 天{flag}")


# ---------------------------------------------------------------------------
# 净值（**权威全收益来源**）
# ---------------------------------------------------------------------------

def fetch_nav(code: str, start: str = "20000101", end: str = "20260915") -> pd.DataFrame:
    """东财 ETF 净值表 → ``time / unit_nav / cum_nav / nav_ret``。

    ⭐ ``nav_ret``（官方「日增长率」/100）是**全收益的唯一权威来源**：
    它在非除息日精确等于单位净值增长率、在除息日多出分红那一块，
    且份额折算日给的是真实市场涨跌（512890 拆分日 −2.16%，而同期单位净值
    与价格都裸跌 −51%）。证据见 ``probe_etf_dividend_dilution.py`` 与
    ``probe_etf_dividend_truth.py``。

    ⚠️ **``cum_nav`` 不能当全收益**：标准定义 ``累计 = 单位 + 累计分红 D``
    ⇒ ``累计比 − 1 = r_true/(1 + D/U)``，是被分红稀释的版本；且它在折算日
    可能被重述（512890 拆分后 累计 = 2×单位）。实测 510880 有 56.8% 交易日
    ``|累计比 − nav_ret| > 5e-4``，而不分红的 512890 偏差恰为 0。
    ``cum_nav`` 只留作 ``EtfNavStore.dilution_residual`` 的诊断输入。

    ⚠️ ``nav_ret`` 是**百分数**，此处经 ``coerce_return_unit`` 统一成**小数**
    后入库。历史上把百分数当小数用导致 7/7 个折算事件全部误判，属于
    "不报错、只静默错"的典型，因此单位纠正在数据入口处做一次、写进
    parquet、下游不再各自假设。
    """
    from aq.data.etf_actions import coerce_return_unit

    ak = _ak()
    raw = ak.fund_etf_fund_info_em(fund=code, start_date=start, end_date=end)
    if raw is None or len(raw) == 0:
        raise ValueError("净值返回空")
    need = ("净值日期", "单位净值", "累计净值", "日增长率")
    miss = [c for c in need if c not in raw.columns]
    if miss:
        raise ValueError(f"净值表缺列 {miss}，实际列 {list(raw.columns)}")
    out = pd.DataFrame({
        "time": pd.to_datetime(raw["净值日期"]),
        "unit_nav": pd.to_numeric(raw["单位净值"], errors="coerce"),
        "cum_nav": pd.to_numeric(raw["累计净值"], errors="coerce"),
        "nav_ret": pd.to_numeric(raw["日增长率"], errors="coerce"),  # 原样
    }).dropna(subset=["time"])
    out = (out.sort_values("time").drop_duplicates(subset=["time"], keep="last")
           .reset_index(drop=True))
    # 单位守卫：**以单位净值为锚**（锚必须在有序序列上算）
    ret, _unit = coerce_return_unit(out["nav_ret"], anchor=out["unit_nav"].pct_change())
    out["nav_ret"] = pd.to_numeric(ret, errors="coerce")
    return out


# ---------------------------------------------------------------------------
# 分红
# ---------------------------------------------------------------------------

def fetch_dividends_sina(code: str) -> pd.DataFrame | None:
    """新浪**逐只**分红表 → ``time / cum_div`` 长表。

    ⚠️ 该接口默认参数就是 ``sh510050``，**必须显式传 symbol**，
    否则会静默返回 510050 的分红给所有标的（本项目栽过的"接口默认值"坑）。
    """
    ak = _ak()
    sym = f"{market_of(code).lower()}{code}"
    raw = ak.fund_etf_dividend_sina(symbol=sym)
    if raw is None or len(raw) == 0:
        return None
    date_col = next((c for c in raw.columns if "日期" in c or c == "date"), None)
    cum_col = next((c for c in raw.columns if "累计" in c), None)
    if date_col is None or cum_col is None:
        raise ValueError(f"分红表列名不认识：{list(raw.columns)}")
    out = pd.DataFrame({
        "time": pd.to_datetime(raw[date_col]),
        "cum_div": pd.to_numeric(raw[cum_col], errors="coerce"),
    }).dropna()
    if out.empty:
        return None
    return (out.sort_values("time").drop_duplicates(subset=["time"], keep="last")
            .reset_index(drop=True))


def fetch_dividends_all(codes: list[str], store: EtfDividendStore,
                        workers: int = 4, force: bool = False) -> tuple[int, int, list[str]]:
    cov = store.coverage()
    todo = [c for c in codes if force or not cov.get(resolve(c))]
    print(f"  分红待拉 {len(todo)} 只（跳过已有 {len(codes) - len(todo)} 只）")
    ok = nodata = 0
    failed: list[str] = []
    t0 = time.time()

    def work(code: str) -> tuple[str, str]:
        for attempt in range(1, 4):
            try:
                dv = fetch_dividends_sina(code)
                if dv is None:
                    return code, "nodata"
                store.save_rows(code, dv)
                return code, f"ok {len(dv)}"
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    return code, f"fail {type(exc).__name__}"
                time.sleep(1.2 * attempt)
        return code, "fail unknown"

    if workers <= 1:
        results = [work(c) for c in todo]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(work, todo))
    for code, st in results:
        if st == "nodata":
            nodata += 1
        elif st.startswith("fail"):
            failed.append(code)
            _log(f"  {resolve(code)} 分红失败: {st}")
        else:
            ok += 1
    print(f"  分红完成：成功 {ok} | 无记录 {nodata} | 失败 {len(failed)} "
          f"| 用时 {time.time() - t0:.1f}s")
    if failed:
        p = PROJECT_ROOT / "data_cache" / "etf_dividend_failed.txt"
        p.write_text("\n".join(failed), encoding="utf-8")
        print(f"  失败清单：{p}")
    return ok, nodata, failed


# ---------------------------------------------------------------------------
# 份额折算（拆分/合并）
# ---------------------------------------------------------------------------

def run_actions(codes: list[str], bar_store: EtfBarStore, action_store,
                workers: int = 6, force: bool = False,
                nav_store: EtfNavStore | None = None) -> tuple[int, int, list[str]]:
    """检测份额折算并回写到日线表。

    三段式，成本是关键考量：

    1. **廉价筛**：只用已落盘的**价格**找 ``|单日收益| > 15%`` 的候选
       （全池 1600+ 只里只有个位数百分比命中）；
    2. **只为候选拉净值**并反解因子（几十只，几十秒），
       顺手把净值**落盘到 ``EtfNavStore``**（候选之外的全池净值由
       ``--nav`` 单独拉，那一步要一个多小时）；
    3. **就地重算**日线表的 ``split_factor / adj_close``（不重新抓行情）。

    ⚠️ 为什么不能跳过第 1 步：全池拉净值实测要一个多小时。
    ⚠️ 只筛出「有跳空」不等于「无折算」—— 小于 15% 的折算与分红无法区分，
    这是有意接受的边界（小折算不会毁掉回测，大折算会）。
    ⚠️ 折算检索是**诊断**，不是收益来源 —— 收益走
    ``EtfNavStore.total_return``（官方日增长率），不依赖本函数是否命中。
    """
    from aq.data.etf_actions import detect_actions, price_jump_candidates

    ns = nav_store if nav_store is not None else EtfNavStore()
    have = bar_store.available()
    by_code = {c.split(".")[0]: c for c in have}
    # 只处理已落盘的（没行情就无从判断跳空）
    targets = [c for c in codes if str(c) in by_code]
    missing = [str(c) for c in codes if str(c) not in by_code]
    print(f"  已落盘可检 {len(targets)} 只；未落盘 {len(missing)} 只（跳过）")

    # --- 阶段 1：价格筛 ---
    cands: list[str] = []
    frames: dict[str, pd.DataFrame] = {}
    for c in targets:
        try:
            df = bar_store.load(c)
        except Exception:  # noqa: BLE001
            continue
        frames[c] = df
        if price_jump_candidates(df):
            cands.append(c)
    print(f"  阶段1 跳空候选 = {len(cands)} 只"
          + (f"：{' '.join(cands)}" if cands else ""))
    if not cands:
        print("  >>> 无候选 → 全部标的因子恒为 1（已落盘表会重算一次以确保一致）")
    sys.stdout.flush()

    # --- 阶段 2：只为候选拉净值并反解 ---
    hit = nodata = 0
    failed: list[str] = []
    if cands:
        def work(code: str) -> tuple[str, list[dict] | str]:
            for attempt in range(1, 4):
                try:
                    nav = fetch_nav(code)
                    ev = detect_actions(code, frames[code], nav)
                    return code, ev, nav
                except Exception as exc:  # noqa: BLE001
                    if attempt == 3:
                        return code, f"fail {type(exc).__name__}: {str(exc)[:70]}", None
                    time.sleep(1.5 * attempt)
            return code, "fail unknown", None

        if workers <= 1:
            results = [work(c) for c in cands]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(work, cands))
        for code, ev, nav in results:
            if isinstance(ev, str):
                failed.append(code)
                _log(f"  ❌ {resolve(code)} 折算检测失败：{ev}")
                continue
            # 顺手把净值落盘：折算检测与全收益用的是同一份数据，不该拉两次
            try:
                ns.save(code, nav)
            except Exception as exc:  # noqa: BLE001
                _log(f"  ❌ {resolve(code)} 净值落盘失败：{type(exc).__name__}: {exc}")
            if ev:
                hit += 1
                for e in ev:
                    _log(f"  ✅ {resolve(code)} {e['prev_time'].date()} → {e['time'].date()} "
                         f"{e['kind']} 因子 {e['factor']:.4f}"
                         f"（价格 {e['r_px']:+.2%} / 净值全收益 {e['r_total']:+.2%}，"
                         f"跨 {e['gap_nav_days']} 个净值日，来源 {e['ret_src']}）")
            else:
                nodata += 1
                _log(f"  ➖ {resolve(code)} 有跳空但反解后不构成折算（可能是真行情/大额分红）")
            action_store.replace_code(code, ev)

    # --- 阶段 3：就地重算因子列 ---
    touched = 0
    for c in targets:
        try:
            bar_store.reapply_actions(c, action_store=action_store)
            touched += 1
        except Exception as exc:  # noqa: BLE001
            _log(f"  ❌ {resolve(c)} 重算复权列失败：{type(exc).__name__}: {exc}")
    print(f"  阶段3 已重算 {touched} 只日线表的 split_factor / adj_close")
    print(f"  折算汇总：命中 {hit} 只 | 有跳空但非折算 {nodata} 只 | 失败 {len(failed)} 只")
    return hit, nodata, failed


def run_nav(codes: list[str], nav_store: EtfNavStore | None = None,
            workers: int = 6, force: bool = False) -> tuple[int, int]:
    """全池拉净值并落盘 —— **这是全收益的唯一来源，必须跑**。

    成本：单只 3~5 秒 → 100 只约 6 分钟、1600 只约 1.2 小时。
    所以默认跳过已存在的；``force=True`` 强制重拉。

    入库时核对不变量 ``EtfNavStore.verify``：``|nav_ret − 单位净值比率|`` 的
    **中位数**必须 ≤ 1e-4。⚠️ 不能拿「``|累计比 − 日增长率|`` 要小」当不变量 ——
    对分红标的它本来就应该大，那样会把全部红利 ETF 误报成脏数据（踩过）。
    ⚠️ 也不能只比"累计比 == 单位比"的那些天 —— 那是退化样本（多为"净值完全
    没动"的 0 vs 0），对 510300/511010 甚至全为 0、灵敏度归零（踩过）。
    """
    ns = nav_store if nav_store is not None else EtfNavStore()
    todo = [c for c in codes if force or not ns.has(c)]
    print(f"[净值] 目标 {len(codes)} 只 | 已有 {len(codes) - len(todo)} 只 | "
          f"待拉 {len(todo)} 只")

    def work(code: str) -> tuple[str, str, dict | None]:
        for attempt in range(1, 4):
            try:
                df = fetch_nav(code)
                ns.save(code, df)
                return code, "ok", ns.verify(code)
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    return code, f"{type(exc).__name__}: {str(exc)[:60]}", None
                time.sleep(1.5 * attempt)
        return code, "unknown", None

    ok = bad = 0
    fails: list[str] = []
    notes: list[tuple[str, float]] = []
    susp: list[tuple[str, float]] = []
    jumps: list[str] = []
    restates: list[tuple[str, int]] = []
    if todo:
        if workers <= 1:
            results = [work(c) for c in todo]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(work, todo))
        for code, st, info in results:
            if st != "ok":
                bad += 1
                fails.append(resolve(code))
                _log(f"  ❌ {resolve(code)} 净值抓取失败：{st}")
                continue
            ok += 1
            # ⚠️ 判据用 **中位数**：``|nav_ret − 单位比|`` 就是"每份分红占前收的
            # 比例"，分红一年才几天，中位数对它们稳健；而单位口径错误会让**全体**
            # 日期一起偏 100 倍 ⇒ 中位数必然爆表。
            # ❌ 不要用 max（会被净值归一日 99.07 与源数据单日孤点占满），
            # ❌ 也不要退回"只在累计比==单位比的日子比较"（那是退化样本，
            #    实测对 510300/511010 全部为 0，灵敏度归零）。
            dev_med = info.get("dev_med", float("nan")) if info else float("nan")
            if dev_med == dev_med:               # 非 NaN
                if dev_med > 1e-2:
                    susp.append((resolve(code), dev_med))
                elif dev_med > 1e-4:
                    notes.append((resolve(code), dev_med))
            elif info and not info.get("ok", True):
                susp.append((resolve(code), float("nan")))
            if info and info.get("n_restate_days"):
                restates.append((resolve(code), int(info["n_restate_days"])))
            if info and info.get("min_implied_d", 0.0) < -1e-3:
                jumps.append(resolve(code))
    print(f"[净值] 成功 {ok} 只 | 抓取失败 {len(fails)} 只")
    if notes:
        print(f"  口径备注：{len(notes)} 只的 |nav_ret − 单位比| 中位在 1e-4~1e-2：")
        print(f"     {'  '.join(f'{c}:{d:.1e}' for c, d in notes)}")
    if susp:
        print(f"  ⚠️ {len(susp)} 只 |nav_ret − 单位比| 中位 > 1e-2 → "
              f"**nav_ret 口径可疑，逐只查**：")
        for c, d in susp:
            print(f"     {c}  {d:.3e}")
    if restates:
        print(f"  同步重述日（单位净值自身的跳变：净值归一/份额折算），"
              f"共 {len(restates)} 只：")
        print(f"     {'  '.join(f'{c}:{n}天' for c, n in restates)}")
        print("     → 中位数判据对它们免疫；逐日原因用 "
              "`scripts/probes/probe_etf_nondiv_outliers.py <code>` 定位")
    if jumps:
        print(f"  ⚠️ {len(jumps)} 只标的的**单位净值存在未记录跳变**"
              f"（隐含分红下限 < −1e-3，多为份额折算或历史重述）：")
        print(f"     {' '.join(jumps)}")
        print("     → 跑 `--actions` 反解折算事件；收益不受影响（走官方日增长率）")
    return ok, len(fails)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def verify() -> int:
    store = EtfBarStore()
    avail = store.available()
    if not avail:
        print("[空] data_cache/etf_bars/ 下没有 ETF 数据")
        return 1
    try:
        lst = load_etf_list()
    except Exception as exc:  # noqa: BLE001
        lst = None
        print(f"[警告] 清单不可读：{exc}")

    print(f"已落盘 {len(avail)} 只 ETF 日线。逐项校验：")
    bad = 0
    residual: list[tuple[str, str, float]] = []
    for name in avail:
        try:
            df = store.load(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  [异常] {name}: {exc}")
            bad += 1
            continue
        rng = f"{df['time'].iloc[0].date()} ~ {df['time'].iloc[-1].date()}"
        kinds = set(df.get("kind", pd.Series(["<无 kind>"])) .dropna().unique())
        cal = set(df.get("price_caliber", pd.Series(["<无标记>"])).dropna().unique())
        note = ""
        if kinds != {"etf"}:
            note += f"  ⚠️ kind={kinds}"
            bad += 1
        if cal != {"raw_no_dividend"}:
            note += f"  ⚠️ close 口径={cal}"
            bad += 1
        if "symbol" not in df.columns:
            note += "  ⚠️ 缺 symbol 列"
            bad += 1
        # --- 折算复权列的不变量 ---
        for c in ("split_factor", "adj_close", "has_action"):
            if c not in df.columns:
                note += f"  ⚠️ 缺 {c}（未跑 --actions）"
                bad += 1
        nsplit = 0
        if "adj_close" in df.columns and "split_factor" in df.columns:
            fac = pd.to_numeric(df["split_factor"], errors="coerce")
            chk = (pd.to_numeric(df["close"], errors="coerce") * fac).values
            got = pd.to_numeric(df["adj_close"], errors="coerce").values
            diff = float(pd.Series(chk - got).abs().max())
            if not diff < 1e-6:
                note += f"  ⚠️ adj_close ≠ close×split_factor（最大差 {diff:.2e}）"
                bad += 1
            nsplit = int((fac.fillna(1.0) != 1).sum())
            # 还原后仍有跳空 → 报出来（可能是真行情，也可能是未识别的折算）
            r = pd.to_numeric(df["adj_close"], errors="coerce").pct_change().abs()
            if len(r) and float(r.max()) > 0.15:
                i = int(r.idxmax())
                residual.append((name, str(df["time"].iloc[i].date()), float(r.max())))
        extra = f"  折算还原 {nsplit} 行" if nsplit else ""
        print(f"  {name:<12} {len(df):>5} 行 | {rng}{extra}{note}")
    if residual:
        print()
        print(f"  ⚠️ {len(residual)} 只在**份额还原后**仍有 |单日收益| > 15% 的日子：")
        for nm, d, v in residual[:25]:
            print(f"      {nm} {d}  {v:.2%}")
        print("      → 逐条判断：真行情 / 大额分红 / 未识别的折算；")
        print("        复算工具：python scripts/probes/probe_etf_split.py")
    if lst is not None:
        have = {resolve(c) for c in avail}
        miss = lst[(lst["n_bars"] > 0) & (~lst["symbol"].isin(have))]
        print(f"清单与落盘一致性：清单有日线 {int((lst['n_bars'] > 0).sum())} 只，"
              f"实际落盘 {len(avail)} 只，清单称有但落盘无 {len(miss)} 只")
    return 0 if bad == 0 else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 清单 / 日线 / 分红抓取")
    ap.add_argument("--list", action="store_true", help="构造 data_cache/etf_list.parquet")
    ap.add_argument("--refresh-dates", action="store_true",
                    help="配合 --list：从已落盘日线回填 list_date")
    ap.add_argument("--nav-check", type=int, default=0,
                    help="配合 --list：抽 N 只做「日线首日 vs 净值首日」交叉校验")
    ap.add_argument("--group", default="", choices=["", *GROUPS.keys()],
                    help="抓哪个篮子（默认 core）")
    ap.add_argument("--codes", nargs="*", default=None, help="显式指定代码")
    ap.add_argument("--all", action="store_true", help="抓全部现役 ETF")
    ap.add_argument("--limit", type=int, default=0, help="限制只数（0=不限）")
    ap.add_argument("--offset", type=int, default=0, help="跳过前 N 只（断点续传）")
    ap.add_argument("--start", default=DEFAULT_START, help="起始日期")
    ap.add_argument("--end", default=DEFAULT_END, help="结束日期")
    ap.add_argument("--workers", type=int, default=4, help="并发线程数（1=串行）")
    ap.add_argument("--force", action="store_true", help="已存在也重拉")
    ap.add_argument("--no-fallback", dest="fallback", action="store_false",
                    help="关闭腾讯→新浪降级")
    ap.set_defaults(fallback=True)
    ap.add_argument("--dividends", action="store_true", help="拉分红（逐只）")
    ap.add_argument("--nav", action="store_true",
                    help="拉净值（**全收益的唯一来源，必跑**；逐只，慢）")
    ap.add_argument("--actions", action="store_true",
                    help="检测份额折算（拆分/合并）并回写复权列")
    ap.add_argument("--crosscheck", type=int, default=0,
                    help="抽 N 只做腾讯 vs 新浪收盘价逐日对比")
    ap.add_argument("--verify", action="store_true", help="只校验，不抓取")
    args = ap.parse_args()

    print("=" * 78)
    print("ETF 数据管道")
    print(f"区间 {args.start} ~ {args.end} | 并发 {args.workers} | 目录 {EtfBarStore().root}")
    print(f"universe_caliber = {UNIVERSE_CALIBER}")
    print("=" * 78)

    if args.verify:
        return verify()

    did_something = False
    if args.list:
        did_something = True
        print("[1] 构造 ETF 清单")
        build_list(refresh_dates=args.refresh_dates, crosscheck_nav=args.nav_check)
        print()

    # ---- 确定要抓的代码 ----
    codes: list[str] = []
    if args.codes:
        codes = [str(c).strip() for c in args.codes if str(c).strip()]
    elif args.all:
        em, sn = _live_sources()
        codes = sorted(_codes_of(em, ("代码",)) | _codes_of(sn, ("代码",)))
    elif args.group or args.refresh_dates or args.dividends or args.actions \
            or args.nav:
        codes = list(GROUPS.get(args.group or "core", GROUPS["core"]))
    if args.offset:
        codes = codes[args.offset:]
    if args.limit:
        codes = codes[: args.limit]

    # ``--nav`` 单独用时不必重抓日线（净值只依赖代码，不依赖行情）；
    # 与 ``--actions`` 同用时日线是折算检测的输入，照抓。
    want_bars = not args.dividends and not (args.nav and not args.actions)
    if codes and want_bars:
        did_something = True
        print(f"[2] 抓日线：{len(codes)} 只")
        store = EtfBarStore()
        todo = [c for c in codes if args.force or not store.has(c)]
        print(f"  跳过已存在 {len(codes) - len(todo)} 只，待拉 {len(todo)} 只")
        ok = fail = 0
        failed: list[str] = []
        t0 = time.time()

        def work(code: str) -> tuple[str, str, str]:
            st, df, note = fetch_one_bars(code, args.start, args.end,
                                          fallback=args.fallback)
            if st != "ok" or df is None:
                return code, "fail", note
            try:
                store.save(code, df)
                return code, "ok", f"{len(df)} 行 {note}"
            except Exception as exc:  # noqa: BLE001
                return code, "fail", f"save:{type(exc).__name__}"

        if args.workers <= 1:
            results = [work(c) for c in todo]
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                results = list(ex.map(work, todo))
        for code, st, note in results:
            if st == "ok":
                ok += 1
                _log(f"  ✅ {resolve(code)} {note}")
            else:
                fail += 1
                failed.append(code)
                _log(f"  ❌ {resolve(code)} {note}")
        el = time.time() - t0
        print("-" * 78)
        print(f"  日线完成：成功 {ok} | 失败 {fail} | 用时 {el:.1f}s"
              f"（{el / max(len(todo), 1):.2f}s/只）")
        if failed:
            p = PROJECT_ROOT / "data_cache" / "etf_fetch_failed.txt"
            p.write_text("\n".join(failed), encoding="utf-8")
            print(f"  失败清单：{p}")
        print()

    if args.dividends:
        did_something = True
        print(f"[3] 抓分红（逐只）：{len(codes)} 只")
        fetch_dividends_all(codes, EtfDividendStore(), workers=args.workers,
                            force=args.force)
        print()

    if args.nav:
        did_something = True
        print(f"[3a] 抓净值（全收益来源）：{len(codes)} 只")
        run_nav(codes, EtfNavStore(), workers=args.workers, force=args.force)
        print()

    if args.actions:
        did_something = True
        print(f"[3b] 份额折算检测：{len(codes)} 只")
        run_actions(codes, EtfBarStore(), EtfActionStore(), workers=args.workers,
                    force=args.force)
        print()

    if args.crosscheck > 0:
        did_something = True
        pool = codes[: args.crosscheck] if codes else GROUPS["core"][: args.crosscheck]
        print(f"[4] 腾讯 vs 新浪 收盘价交叉校验（{len(pool)} 只）")
        print(f"  {'代码':<10}{'重叠天数':>9}{'最大相对差':>12}{'平均相对差':>12}"
              f"{'首日一致':>10}{'末日一致':>10}")
        worst = 0.0
        for code in pool:
            try:
                r = crosscheck_bars(code, args.start, args.end)
            except Exception as exc:  # noqa: BLE001
                print(f"  {resolve(code):<10} 失败 {type(exc).__name__}")
                continue
            worst = max(worst, r["max_rel"] if r["max_rel"] == r["max_rel"] else 0.0)
            print(f"  {resolve(code):<10}{r['n_overlap']:>9}{r['max_rel']:>12.2e}"
                  f"{r.get('mean_rel', float('nan')):>12.2e}"
                  f"{str(r['first_same']):>10}{str(r['last_same']):>10}")
        print(f"  >>> 全部标的最大相对差 = {worst:.2e}")
        print("  >>> 判据：最大相对差 < 1e-6 才算同口径；大于 1e-3 说明有一源复权了。")
        print()

    if not did_something:
        print("什么都没做。用 --list / --group / --all / --dividends / --verify 指定动作。")
        return 1

    print("=" * 78)
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
