"""ETF 公司行为（份额折算）的检测与复权。

为什么需要它
------------
ETF 的二级市场价格序列**含公司行为造成的不连续**，直接用会毁掉回测：

    512890 红利低波ETF
      2021-10-22  基金份额 1:2 拆分（证监会基金电子披露网站公告）
      2021-10-25  价格 1.6390 → 0.8010，**单日 −51.13%**
      同日官方净值「日增长率」= **−2.16%**

把它当亏损算 → 价格年化 2.32%；按份额还原 → ≈12.4%。**差 10pp/年**，
而它是 B 路线（红利低波）的核心标的。同类事件还检出：515880（1:3）、
588710（1:3）、159516（1:2）、159845（份额合并 ≈1:2.85）。

判据（第三版）
-------------
    factor = (1 + r_total) / (1 + r_px)
    r_total = Π_{净值日 ∈ (t_prev, t]} (1 + r_i)，r_i = 官方「日增长率」/100

⚠️ **为什么不能用「价格日收益 vs 单位净值日收益」比较**（第一版的错）
折算日 ETF 通常**停牌**（512890 拆分日 2021-10-22 停牌），价格序列根本没有
那一行，于是价格跳空与净值跳空落在**同一个跨日区间**里、同号同幅、互相抵消
→ 该判据会漏掉全部停牌型拆分，并给真折算算出 factor=1.0。
用「净值侧全收益累乘」就没有这个问题：它是逐日的，跨停牌多日也能正确累乘。

❌ **第二版栽在单位上（教训，必读）**
东财「日增长率」是**百分数**（``−2.16`` 表示 −2.16%）。第二版把它当小数用，
于是 7/7 个事件全部误判，且 ``factor`` 系统性偏大：

| 标的 | 事件日 | 官方日增长率 | 误当小数得到 | 真值 |
|---|---|---|---|---|
| 512890 | 2021-10-22 | ``−2.16`` | 被 ``>−1.0`` 当脏值丢弃 → +22% | **2.006（1:2 拆分）** |
| 159915 | 2024-10-08 | ``17.23`` | 1+17.23 = 18.23 → factor 15.19 | 无折算 |
| 510500 | 2015-04-15 | ``2.14`` / ``−0.04`` | 3.14×0.96 = 3.01 → factor 0.86 | **≈3.55 份额折算** |

因为这种错误**不会报错、只会静默给错因子**，本模块加了
``coerce_return_unit()`` 守卫：**以单位净值为锚**判定整列单位并自动修正
（``nav_total_return`` 负责把锚传进去），修正结果与 ``ret_unit`` 一起写进事件行。

⚠️ **第三版中途还错过一次：误把「累计净值比率」当全收益**（已纠正）。
设标准定义 ``累计净值 = 单位净值 + 累计分红 D``，则
``累计比 − 1 = r_true / (1 + D/U)`` —— 累计比是被累计分红**稀释**的版本，
且它在份额折算日还可能被重述（512890 拆分后 累计 = 2×单位）。
实测 510880 有 56.8% 交易日 ``|累计比 − 官方日增长率| > 5e-4``，
而不分红的 512890 偏差恰好为 0 —— 偏差只出现在分红标的上。
**结论：净值侧收益一律取官方日增长率，详见 ``nav_total_return``。**

⚠️ **分红也会让 factor > 1**（分红日价格下跌，而净值侧收益已含分红），
量级 = 每份分红 / 价格。红利类 ETF 单次大额分红约 5%、月频分红约 0.5%。
因此默认阈值取 **10%** —— 只抓大事件，正好是唯一会毁掉回测的那一类；
小比例折算与分红不做区分，**这是有意的取舍**。

⭐ **收益本身不必依赖本模块**：见 ``EtfNavStore.total_return``。
本模块的产出（``factor`` / ``adj_close``）主要服务于**价格路径**
（成交额、撮合、技术指标），以及**数据质量告警**（哪只标的发生了未预期的份额变动）。

用法::

    from aq.data.etf_actions import EtfActionStore, detect_actions, restore_factor

    ev = detect_actions("512890", px_df, nav_df)      # 反解事件
    store = EtfActionStore(); store.replace_code("512890", ev)
    f = restore_factor(index, ev)                     # 逐日累乘因子
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from aq.config.settings import PROJECT_ROOT

#: ``data_cache/etf_actions.parquet`` 的契约列。
ACTION_COLS = (
    "symbol",        # 510300.SH 形式
    "time",          # 价格跳空日 = 折算后首个交易日（因子自该日起生效）
    "prev_time",     # 折算前最后一个交易日
    "kind",          # split（拆分，factor>1）/ merge（合并，factor<1）
    "factor",        # 反解出的份额还原因子；(1 + r_total) / (1 + r_px)
    "factor_simple", # 若贴近简单整数比则填该整数比，否则 NaN
    "r_px",          # 价格跨日收益
    "r_total",       # 净值侧全收益累乘（已含分红、折算中性）
    "gap_nav_days",  # 跨了几个净值日（>1 说明中间停牌）
    "ret_src",       # 净值侧收益来源：cum_nav / nav_ret
    "ret_unit",      # 净值侧收益的原始单位：fraction / percent_converted
    "src",           # 判定来源标记
    "detected_at",
)

#: 价格单日跳空超过它才进入候选 —— 先廉价筛，只为候选拉净值。
CANDIDATE_PX_JUMP = 0.15

#: |factor − 1| 超过它才判为折算。10% 是为了避开分红（见模块 docstring）。
DEFAULT_FACTOR_THR = 0.10

#: 反解因子与简单整数比的相对距离在它以内 → 认定是干脆的整数比。
DEFAULT_SIMPLE_TOL = 0.06

#: 可识别的简单整数比（份额合并给 <1，拆分给 >1）。
SIMPLE_RATIOS = (1 / 10, 1 / 8, 1 / 5, 1 / 4, 1 / 3, 1 / 2,
                 2.0, 3.0, 4.0, 5.0, 8.0, 10.0)


def default_actions_path() -> Path:
    return PROJECT_ROOT / "data_cache" / "etf_actions.parquet"


def _resolve(code: str) -> str:
    # 延迟导入避免与 etf_store 形成循环依赖
    from aq.data.etf_store import resolve

    return resolve(code)


def _simple_ratio(factor: float,
                  tol: float = DEFAULT_SIMPLE_TOL) -> float | None:
    for r in SIMPLE_RATIOS:
        if abs(factor - r) / r <= tol:
            return r
    return None


#: 无锚时的兜底判据：99 分位 |r| 超过它且中位 |r| 也超过 ``FALLBACK_MED``
#: 才判百分数。两个条件缺一不可 —— 单用分位数会被个别巨额脏值带偏，
#: 单用中位数会把**货币 ETF**（日涨幅 ~0.005，百分数口径下也是 0.005）漏判。
UNANCHORED_Q99 = 0.30
UNANCHORED_MED = 0.05

#: 锚判定所需的最少有效锚点数；少于它就交给量级闸门。
MIN_ANCHOR_POINTS = 20

#: **决定性倍数**：胜出假设的拟合优度须比「原样」好这么多倍才承认它。
#: 见 ``coerce_return_unit`` 里「为什么需要决定性倍数」一段 —— 没有它，
#: 货币 ETF 那种「信号落在舍入精度以下」的标的会在第二次守卫时被再除一次 100。
ANCHOR_DECISIVE_RATIO = 10.0

#: 量级闸门：年化绝对值不超过它才算"可信的收益量级"。
PLAUSIBLE_ANN = 1.0

#: **净值归一 / 份额折算**的判据：单位净值比率的绝对值超过它即为同步重述日。
#: 这类天累计与单位净值**同步**跳变（511880 在 2013-04-03 把 1.0000 归一为
#: 100.074），所以「``|累计比 − 单位比|`` 很小 ⇒ 当日无分红」这条推理
#: 对它们**失效** —— 必须显式排除，否则任何基于该掩码的 ``max`` 统计
#: 都会被这一天占满（实测 99.07 vs 真实不变量 5e-05）。
NAV_RESTATE_ABS_RATIO = 0.15


def _ann_gross(x: pd.Series) -> float:
    v = pd.Series(x).dropna()
    if len(v) < 30:
        return float("nan")
    return float((1.0 + v).prod() ** (250.0 / len(v)) - 1.0)


def coerce_return_unit(s: pd.Series,
                       anchor: pd.Series | None = None) -> tuple[pd.Series, str]:
    """把净值侧收益统一成**小数**，并报告原始单位。

    东财「日增长率」是百分数（``−2.16`` = −2.16%）。第二版把「日增长率」
    直接当小数用 → 7/7 个折算事件全错、且静默无异常。本函数是那道守卫。

    级联判据（**前一级判不出来才走下一级**）:

    **① 以单位净值为锚**（首选）。``anchor`` = 单位净值比率。在**非除息日**
    上官方日增长率**必须**等于单位净值增长率，所以直接比三种假设的中位绝对
    偏差，取最小的：

    - ``|anchor − r|``     → 原样（已是小数）
    - ``|anchor − r/100|`` → 百分数，需 /100
    - ``|anchor − r×100|`` → 写成了比例（罕见）

    实现上不显式挑除息日（那要 ``cum_nav``，守卫不一定拿得到），而是对
    **全样本取中位数** —— 分红一年只有 1~12 天，中位数对它们是稳健的。

    ⭐ **胜出假设必须"决定性"**（``ANCHOR_DECISIVE_RATIO`` = 10 倍），
    不能"好一点点"就改。理由见末尾「为什么需要决定性倍数」。
    不决定性时**保持原样**并交回 ②/③。

    **② 量级闸门**（锚退化时）。锚点数 < ``MIN_ANCHOR_POINTS``，或三档误差
    **全为 0**（平局 —— 债基的锚点日常常单位净值一动不动，三档都拟合得很好，
    这时锚**没有信息量**，不能拿平局当结论），或 ① 判为不决定性。此时看哪种
    假设的年化收益落在可信区间 ``|ann| ≤ PLAUSIBLE_ANN``：
    原样不合理而 /100 合理 → 取 /100。

    **③ 兜底**（无量级信息）→ 原样，不做事。

    ⚠️ 实测三种必须都有的情形（``probe_etf_unit_guard.py``）：
    | 标的 | 中位\\|r\\| | 锚判 | 只用① | 只用② |
    |---|---|---|---|---|
    | 511880 货币ETF | 0.010 | ÷100 | ✓ | ✗（0.01 落在灰色带） |
    | 511010 国债ETF | 0.050 | ÷100 | ✗（平局） | ✓ |
    | 510300 沪深300 | 0.630 | ÷100 | ✗（平局） | ✓ |
    | 512890 红利低波 | 0.530 | ÷100 | ✓ | ✓ |
    → 单靠任何一路都会错，级联才对。

    返回 ``(已换算的序列, 单位标记)``，标记 ∈
    ``{fraction, percent_converted, ratio_scaled_up, ambiguous_kept}``。
    **幂等**：已判为小数的输入不会再被除一次。

    ⭐ **为什么需要决定性倍数**（``ANCHOR_DECISIVE_RATIO``）
    ------------------------------------------------
    「信号幅度落在数据舍入精度以下」时，锚点的判别力会归零，而"最小的那个"
    仍然会被选出来 —— 于是每次经过守卫都可能再除一个 100。实例如货币 ETF：

    - 真实日收益 ≈ 0.005% ⇒ 小数 5e-5；
    - 但官方「日增长率」只保留 **2 位小数**，原始值被量化为 ``0.01``
      （= 1e-4），舍入步长与信号**同量级**；
    - 第一次守卫：``|anchor − 0.01|`` = 9.95e-3 vs ``|anchor − 1e-4|``
      = 5e-5 → 好 199 倍 → 决定性 ✓ → 换算成 1e-4，正确；
    - **第二次守卫**（``save`` 会再跑一遍）：``|anchor − 1e-4|`` = 5e-5
      vs ``|anchor − 1e-6|`` = 4.9e-5 → 只"好" 1.02 倍，纯属舍入噪声，
      却仍然判 ``/100`` → **再除一次 100**，静默错 100 倍。

    加上"必须好 >= 10 倍"后，第二次判为不决定性 → 落到 ②/③ → ③ 见
    ``|r|`` 99 分位远低于 ``UNANCHORED_Q99`` → 原样放行，幂等成立。
    **分辨率不足的代价已经付在源数据里**（2 位小数量化），守卫不该把它翻倍。

    ⚠️ **换算系数方向（踩过一次，必读）**：系数作用在**输入**上。
    ``percent_converted`` ⇒ ``s × 0.01``（不是 ×100）；``ratio_scaled_up``
    ⇒ ``s × 100``。写反的后果是**判定正确、数值错 100 倍、且不抛异常** ——
    在下游只表现为 ``verify`` 的 ``nondiv_dev_max`` 从 1e-5 变成 1e6。
    改动本函数后**必须**跑 ``scripts/probes/probe_etf_guard_roundtrip.py``
    （合成数据 save→load 往返 + 幂等 + 量级三判据）。
    """
    v = pd.to_numeric(s, errors="coerce")
    raw = v.dropna()
    if len(raw) == 0:
        return s, "fraction"

    # ---------- ① 锚判定 ----------
    if anchor is not None:
        a = pd.to_numeric(anchor, errors="coerce")
        idx = raw.index.intersection(a.dropna().index)
        if len(idx) >= MIN_ANCHOR_POINTS:
            av = a.reindex(idx)
            rv = raw.reindex(idx)
            e = {
                "fraction": float((av - rv).abs().median()),
                "percent_converted": float((av - rv / 100.0).abs().median()),
                "ratio_scaled_up": float((av - rv * 100.0).abs().median()),
            }
            best = min(e, key=lambda k: e[k])
            # 平局（三档全 0）说明锚点没有信息量 → 交给量级闸门
            if e[best] > 0 or any(x > 0 for x in e.values()):
                # e[best] == 0 时"好多少倍"是无穷 —— 显式给 inf，别让它去触发
                # ZeroDivisionError（浮点除零会**抛异常**，不是返回 nan）。
                gain = (e["fraction"] / e[best]) if e[best] > 0 else float("inf")
                # ⭐ 决定性倍数：证据必须"好很多"，不能"好一点点"。
                # 缺了这道闸门，货币 ETF 会不幂等 —— 见函数 docstring 末段。
                decisive = (best != "fraction" and e["fraction"] > 0
                            and gain >= ANCHOR_DECISIVE_RATIO)
                if decisive:
                    # ⚠️ 系数是**作用在输入上的**，不是"真值 = 输入 × 系数"：
                    #    percent_converted 的语义是「输入是百分数数，真值 = r/100」
                    #    ⇒ 因子 0.01；ratio_scaled_up 反之 ⇒ 因子 100。
                    #    这里写反过一次（判对了却乘 100 入库），后果是收益偏
                    #    100 倍且**不报错**，只在 ``verify`` 里表现为
                    #    nondiv_dev_max 爆到 1e6~1e7、全体标的被误报"净值有跳变"。
                    #    往返护栏：``scripts/probes/probe_etf_guard_roundtrip.py``。
                    f = {"percent_converted": 0.01,
                         "ratio_scaled_up": 100.0}[best]
                    op = "÷100（取百分数之真值）" if best == "percent_converted" \
                        else "×100（取比例之真值）"
                    print(f"  [守卫①锚] 净值侧收益判为百分数"
                          f"（中位偏差 {e['fraction']:.2e} → {e[best]:.2e}，"
                          f"好 {gain:.0f} 倍，{len(idx)} 天比锚）→ 已 {op}。")
                    return s * f, best
                if best == "fraction":
                    return s, "fraction"
                print(f"  [守卫①锚] 锚点**无法决定性区分**单位（原样 "
                      f"{e['fraction']:.2e} vs 换算后 {e[best]:.2e}，仅好 "
                      f"{gain:.1f} 倍 < {ANCHOR_DECISIVE_RATIO:.0f} 倍）"
                      f"→ 保持原样，转量级闸门。")
                # 落到 ②/③ —— 那里的判据对"已换算过"的数据会原样放行
            print(f"  [守卫①锚] {len(idx)} 个锚点三档误差全为 0（锚点无信息量，"
                  f"多为债基/货币 ETF）→ 转量级闸门。")

    # ---------- ② 量级闸门 ----------
    a0, a1 = _ann_gross(raw), _ann_gross(raw / 100.0)
    ok0, ok1 = abs(a0) <= PLAUSIBLE_ANN, abs(a1) <= PLAUSIBLE_ANN
    if ok1 and not ok0:
        print(f"  [守卫②量级] 原样年化 {a0:.1%} 不合理、/100 得 {a1:.1%} 合理"
              f" → 已 /100。")
        return s / 100.0, "percent_converted"
    if ok0 and not ok1:
        return s, "fraction"
    if not ok0 and not ok1:
        print(f"  [守卫②量级] 两种假设年化都不合理（原样 {a0:.1%} / ÷100 "
              f"{a1:.1%}）→ 取 /100 并**请人工复核该标的净值列**。")
        return s / 100.0, "percent_converted"

    # ---------- ③ 兜底 ----------
    q99 = float(raw.abs().quantile(0.99))
    med = float(raw.abs().median())
    if q99 > UNANCHORED_Q99 and med > UNANCHORED_MED:
        print(f"  [守卫③兜底] 99 分位 {q99:.3f} > {UNANCHORED_Q99} 且中位 "
              f"{med:.3f} > {UNANCHORED_MED} → 判百分数，已 /100。")
        return s / 100.0, "percent_converted"
    return s, "ambiguous_kept"


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------

def price_jump_candidates(px: pd.DataFrame, jump: float = CANDIDATE_PX_JUMP) -> list:
    """廉价筛：只看价格，返回 |单日收益| > ``jump`` 的日期列表。

    生产管线的成本控制点：全池 1600+ 只里只有个位数百分比会命中，
    因此只为命中者拉净值（否则全池拉净值要一个多小时）。
    """
    if px is None or len(px) < 30:
        return []
    p = pd.Series(pd.to_numeric(px["close"], errors="coerce").values,
                  index=pd.to_datetime(px["time"])).dropna().sort_index()
    r = p.pct_change()
    return [t for t in r.index[r.abs() > jump]]


#: 净值侧全收益序列的来源标记。
RET_SRC_NAV_RET = "nav_ret"        # ⭐ 官方「日增长率」= 全收益（首选）
RET_SRC_CUM_NAV = "cum_nav_diluted"  # 累计净值比率：被累计分红稀释，**仅退化**


def nav_total_return(nav: pd.DataFrame) -> tuple[pd.Series, str, str]:
    """从净值表构造**净值侧全收益日收益**（小数），返回 ``(ret, src, unit)``。

    ⭐ 首选 ``nav_ret`` = 官方「日增长率」/100，它就是全收益（含分红、折算中性）。
    证据（``probe_etf_dividend_dilution.py``，9 只标的）：

    - **非除息日残差中位 = 0**（510880/510300/511010 精确为 0，其余 ≈2.5e-5
      即日增长率只保留 2 位小数的舍入）→ 官方日增长率在普通日**等于**
      单位净值增长率；
    - 除息日残差 ≠ 0 且为正 → 它比单位净值增长率**多出分红**那一块。

    两条合起来 ⇒ 官方日增长率 = 单位净值增长率 + 分红/前收 = **全收益**。
    年化差异也吻合：515080 官方 11.28% vs 单位比 7.14%（差 4.14pp ≈ 中证红利
    股息率）；单纯不分红的 159915 两者几乎相同（9.57% vs 8.52%，差在首日）。

    ❌ **不要用累计净值比率当全收益**（本模块一度这样做，已纠正）。
    设标准定义 ``累计净值 = 单位净值 + 累计分红 D``，则

        累计比 − 1 = r_true / (1 + D_{t−1}/U_{t−1})

    即**累计比是 r_true 被 D/U 稀释后的版本**。实测：510880 有 56.8% 的交易
    日 ``|累计比 − 官方日增长率| > 5e-4``（max 0.0199），而 512890（不分红、
    且累计被重述为 2×单位）偏差恰好为 0 —— 偏差**只出现在分红标的上**。
    另外累计净值在份额折算日也可能被重述（512890 拆分后 累计 = 2×单位），
    所以它连"折算中性"都不保证。

    ⚠️ 单位守卫：``nav_ret`` 若整列像百分数会被 ``coerce_return_unit`` 自动
    /100 并打印警告。这条守卫是必需的 —— 把百分数当小数用不报错，
    只会静默给出错 100 倍的收益（本项目已踩过一次，7/7 折算事件全错）。
    """
    if nav is None or nav.empty:
        raise ValueError("净值表为空")
    cols = set(nav.columns)
    idx = pd.to_datetime(nav["time"])

    if "nav_ret" in cols:
        raw = pd.Series(pd.to_numeric(nav["nav_ret"], errors="coerce").values,
                        index=idx).dropna().sort_index()
        # ⭐ 带锚判定单位：锚 = 单位净值比率（净值表自带，与本判据同源自洽）
        anchor = None
        if "unit_nav" in cols:
            un = pd.Series(pd.to_numeric(nav["unit_nav"], errors="coerce").values,
                           index=idx).dropna().sort_index()
            anchor = un.pct_change()
        r, unit = coerce_return_unit(raw, anchor=anchor)
        if len(r):
            return r.dropna(), RET_SRC_NAV_RET, unit

    if "cum_nav" in cols:
        print("  [警告] 净值表无可用 nav_ret（官方日增长率），退化用累计净值比率 —— "
              "该口径被累计分红稀释，**不是全收益**，只在无选择时使用。")
        cum = pd.Series(pd.to_numeric(nav["cum_nav"], errors="coerce").values,
                        index=idx).dropna().sort_index()
        return cum.pct_change().dropna(), RET_SRC_CUM_NAV, "fraction"

    raise ValueError(
        f"净值表既无 nav_ret 也无 cum_nav，实际列 = {sorted(cols)}"
    )


def detect_actions(code: str, px: pd.DataFrame, nav: pd.DataFrame,
                   jump: float = CANDIDATE_PX_JUMP,
                   factor_thr: float = DEFAULT_FACTOR_THR,
                   simple_tol: float = DEFAULT_SIMPLE_TOL) -> list[dict]:
    """反解 ``code`` 的份额折算事件。

    ``nav`` 需含 ``time`` 与 ``cum_nav``（首选）或 ``nav_ret``（退化）。
    **两条都缺就抛 ``ValueError``**，而不是静默返回空列表 —— 空列表的
    含义必须是「在跳空候选里确实没有折算」，不能是「我没数据」。

    ⚠️ 返回空列表**不是**「这只 ETF 没有折算」：小于 ``jump`` 阈值的
    份额变动与分红无法区分，这是有意的取舍。
    """
    if px is None or nav is None or len(px) < 30 or len(nav) < 30:
        return []
    r_total, ret_src, ret_unit = nav_total_return(nav)
    r_total = r_total[r_total > -1.0]      # 剔除 −100% 脏值（log 会炸）
    if len(r_total) == 0:
        return []
    lr = np.log1p(r_total)                 # 对数收益，便于跨停牌区间求和

    p = pd.Series(pd.to_numeric(px["close"], errors="coerce").values,
                  index=pd.to_datetime(px["time"])).dropna().sort_index()

    now = pd.Timestamp.now()
    events: list[dict] = []
    for t in price_jump_candidates(px, jump):
        prev = p.index[p.index < t]
        if len(prev) == 0:
            continue
        t0 = prev[-1]
        px_ret = float(p[t] / p[t0] - 1.0)
        if abs(1.0 + px_ret) < 1e-9:
            continue
        seg = lr[(lr.index > t0) & (lr.index <= t)]
        if len(seg) == 0:
            continue
        r_nav_total = float(np.expm1(seg.sum()))
        factor = (1.0 + r_nav_total) / (1.0 + px_ret)
        if abs(factor - 1.0) <= factor_thr:
            continue
        simple = _simple_ratio(factor, simple_tol)
        events.append({
            "symbol": _resolve(code),
            "time": t,
            "prev_time": t0,
            "kind": "split" if factor > 1 else "merge",
            "factor": float(factor),
            "factor_simple": float(simple) if simple is not None else float("nan"),
            "r_px": px_ret,
            "r_total": r_nav_total,
            "gap_nav_days": int(len(seg)),
            "ret_src": ret_src,
            "ret_unit": ret_unit,
            "src": "nav_total_return_vs_price",
            "detected_at": now,
        })
    return sorted(events, key=lambda e: e["time"])


# ---------------------------------------------------------------------------
# 复权
# ---------------------------------------------------------------------------

def restore_factor(index, events: list[dict]) -> pd.Series:
    """逐日累乘的份额还原因子（**只含折算，不含分红**）。

    语义：``close_adj = close × restore_factor`` 得到「以最初份额为基准」的
    连续价格序列。等价于真实持有人经历折算后的收益（份额翻倍、价格减半）。

    事件在 ``time`` 当天生效（那天价格已是折算后的）。
    """
    idx = pd.DatetimeIndex(pd.to_datetime(list(index)))
    f = pd.Series(1.0, index=idx)
    for e in sorted(events, key=lambda x: x["time"]):
        t = pd.Timestamp(e["time"])
        f.loc[idx >= t] = f.loc[idx >= t] * float(e["factor"])
    return f


def apply_restore(df: pd.DataFrame, events: list[dict] | None) -> pd.DataFrame:
    """给日线表加上 ``split_factor`` / ``adj_close`` / ``has_action`` 三列。

    只调整价格列，**不动 volume / amount** —— 量额是原始成交记录，
    调整它们会把「名义换手」搞乱（成交额门槛要用真实成交额）。
    """
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"])
    if not events:
        out["split_factor"] = 1.0
        out["adj_close"] = pd.to_numeric(out["close"], errors="coerce")
        out["has_action"] = False
        return out
    f = restore_factor(out["time"], events)
    out["split_factor"] = f.values
    out["adj_close"] = pd.to_numeric(out["close"], errors="coerce") * out["split_factor"]
    out["has_action"] = out["split_factor"] != 1.0
    return out


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

def _atomic_write(df: pd.DataFrame, path: Path) -> Path:
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


class EtfActionStore:
    """``data_cache/etf_actions.parquet`` 的读写封装（长表，一只多行）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_actions_path()
        if not self.path.is_absolute():
            self.path = PROJECT_ROOT / self.path

    def load(self) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame(columns=list(ACTION_COLS))
        df = pd.read_parquet(self.path)
        df["time"] = pd.to_datetime(df["time"])
        if "prev_time" in df.columns:
            df["prev_time"] = pd.to_datetime(df["prev_time"])
        return df.sort_values(["symbol", "time"]).reset_index(drop=True)

    def load_or_none(self, code: str) -> list[dict] | None:
        df = self.load()
        if df.empty:
            return None
        sub = df[df["symbol"] == _resolve(code)]
        return sub.to_dict("records") if len(sub) else None

    def replace_code(self, code: str, events: list[dict]) -> None:
        """整只替换（幂等）。空列表 = 明确记为"无事件"（会删掉旧记录）。"""
        cur = self.load()
        cur = cur[cur["symbol"] != _resolve(code)] if not cur.empty else cur
        rows = pd.DataFrame(events) if events else pd.DataFrame(columns=list(ACTION_COLS))
        if not rows.empty:
            for c in ACTION_COLS:
                if c not in rows.columns:
                    rows[c] = pd.NA
            rows = rows[list(ACTION_COLS)]
        merged = pd.concat([cur, rows], ignore_index=True) if not cur.empty else rows
        if merged.empty:
            merged = pd.DataFrame(columns=list(ACTION_COLS))
        else:
            merged = (merged.sort_values(["symbol", "time"])
                      .drop_duplicates(subset=["symbol", "time"], keep="last")
                      .reset_index(drop=True))
        _atomic_write(merged, self.path)

    def summary(self) -> pd.DataFrame:
        df = self.load()
        if df.empty:
            return df
        cols = [c for c in ("symbol", "time", "kind", "factor", "factor_simple",
                            "r_px", "r_total", "gap_nav_days", "ret_src",
                            "ret_unit") if c in df.columns]
        return df[cols].reset_index(drop=True)
