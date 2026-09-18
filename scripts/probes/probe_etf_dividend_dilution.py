"""判别：东财「累计净值比率」与「官方日增长率」**谁是权威全收益**。

为什么需要这个判别
------------------
上一轮（2026-09-18 早）我在 7 个折算事件的窄窗口上看到「累计净值比率 ==
官方日增长率」，就下了结论「累计净值序列就是全收益指数」。大样本一验，
**7 只标的上不成立**（510880 有 57% 的交易日偏差 >5e-4，max 0.0199）。

两种候选口径的数学形态不同，可以判别：

设单位净值 ``U``、当日每份分红 ``d``、累计分红 ``D = Σd``（同份额基准）。
若东财「累计净值」= ``U + D``（标准定义），则

    累计比 − 1 = (ΔU + d) / (U_{t−1} + D_{t−1})
               = r_true · U_{t−1} / (U_{t−1} + D_{t−1})
               = r_true / (1 + D_{t−1}/U_{t−1})

→ **累计比是 r_true 的「稀释版」**，稀释因子恒为 ``1/(1+D/U)``，与 r 无关。
→ 推论 1：残差 ``|累计比 − 官方日增长率| ≈ |r| · (D/U)/(1+D/U)``，
   **与 |r| 成正比**（这是可检验的强结构预测）。
→ 推论 2：``D/U`` 可由 ``r/残差 − 1`` 反解，且**应当长期稳定**。

替代假设「官方日增长率只是单位净值增长率（不含分红）」会被立刻否证：
那样残差 = ``|累计比 − 单位比|``，在**非除息日恒为 0**，
不可能出现 57% 的交易日都有偏差。

本探针就做这两条结构检验。

用法::

    python scripts/probes/probe_etf_dividend_dilution.py
"""

from __future__ import annotations

import os
import sys
import time
import warnings
from pathlib import Path

os.environ.setdefault("TQDM_DISABLE", "1")

_here = Path(__file__).resolve()
_root = next(p for p in _here.parents if (p / "config" / "base.yaml").exists())
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

warnings.filterwarnings("ignore")

import akshare as ak  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

pd.set_option("display.width", 220)

#: 分红风格对照：左边该分红（稀释应显著），右边基本不分红（稀释应≈0）
CASES = [
    ("512890", "红利低波（B 路线核心）"),
    ("510880", "红利ETF（高分红）"),
    ("515180", "红利ETF易方达"),
    ("515080", "中证红利ETF"),
    ("563020", "低波红利ETF"),
    ("511010", "国债ETF（分红）"),
    ("510300", "沪深300ETF"),
    ("159915", "创业板ETF（不分红）"),
    ("513500", "标普500ETF"),
]

#: 参与结构检验的最小 |r|（低于它是舍入噪声，除法会炸）
MIN_R = 5e-4


def load_nav(code: str, tries: int = 4) -> pd.DataFrame:
    """东财净值表 → ``time / unit_nav / cum_nav / nav_ret``（nav_ret 已是小数）。

    ⚠️ 官方「日增长率」是**百分数**（−2.16 = −2.16%），必须 /100。
    把百分数当小数用是本项目栽过的坑：不报错，只静默给出错 100 倍的收益。
    """
    last: Exception | None = None
    for k in range(1, tries + 1):
        try:
            raw = ak.fund_etf_fund_info_em(fund=code, start_date="20000101",
                                           end_date="20260915")
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * k)
    else:
        raise RuntimeError(f"{code} 拉净值失败：{type(last).__name__}: {last}")
    if raw is None or len(raw) == 0:
        raise RuntimeError(f"{code} 净值返回空")
    df = pd.DataFrame({
        "time": pd.to_datetime(raw["净值日期"]),
        "unit_nav": pd.to_numeric(raw["单位净值"], errors="coerce"),
        "cum_nav": pd.to_numeric(raw["累计净值"], errors="coerce"),
        "nav_ret": pd.to_numeric(raw["日增长率"], errors="coerce") / 100.0,
    }).dropna(subset=["time"])
    return (df.sort_values("time").drop_duplicates(subset=["time"], keep="last")
            .set_index("time").reset_index())


def ann(s: pd.Series) -> float:
    s = s.dropna()
    if len(s) < 30:
        return float("nan")
    return float((1.0 + s).prod() ** (250.0 / len(s)) - 1.0)


def sharpe(s: pd.Series) -> float:
    s = s.dropna()
    if len(s) < 30 or s.std(ddof=1) == 0:
        return float("nan")
    return float(s.mean() / s.std(ddof=1) * np.sqrt(250.0))


def analyse(code: str, note: str) -> dict | None:
    try:
        n = load_nav(code).set_index("time")
    except Exception as exc:  # noqa: BLE001
        print(f"  {code} 失败：{exc}")
        return None

    r_rep = n["nav_ret"]                       # 官方日增长率（复权，权威候选）
    r_cum = n["cum_nav"].pct_change()          # 累计净值比率（稀释候选）
    r_unit = n["unit_nav"].pct_change()        # 单位净值比率（不分红候选）
    resid = (r_cum - r_rep).abs()

    # 判别 1：替代假设否证 —— 官方日增长率若 == 单位比，则非除息日残差应为 0
    non_div_like = (r_unit - r_cum).abs() < 1e-9     # 累计比 == 单位比 的日（非除息日）
    resid_on_nondiv = resid[non_div_like].dropna()

    # 判别 2（核心）：残差 ∝ |r|，比例 = g/(1+g)，g = D/U
    both = pd.DataFrame({"r": r_rep, "resid": resid}).dropna()
    both = both[both["r"].abs() > MIN_R]
    ratio = (both["r"].abs() / both["resid"]).replace([np.inf, -np.inf], np.nan).dropna()
    if len(both) > 30 and both["r"].abs().std() > 0:
        corr = float(np.corrcoef(both["r"].abs(), both["resid"])[0, 1])
        # 稳健斜率：残差 / |r| 的中位数 = g/(1+g)
        slope = float((both["resid"] / both["r"].abs()).median())
        g = slope / (1.0 - slope) if slope < 1 else float("nan")
    else:
        corr, slope, g = float("nan"), float("nan"), float("nan")

    # 累计分红的直接读数：gap = cum − unit
    gap = (n["cum_nav"] - n["unit_nav"])
    gap_med = float(gap.median())
    unit_med = float(n["unit_nav"].median())
    g_direct = gap_med / unit_med if unit_med else float("nan")

    # 隐含每份分红（用官方日增长率反解，clip ≥0）
    d_impl = (n["unit_nav"].shift(1) * (1.0 + r_rep) - n["unit_nav"]).clip(lower=0.0)
    ex_days = int((d_impl > 0.005).sum())

    return {
        "code": code,
        "标的": note,
        "n": len(n),
        "残差>5e-4 占比": float((resid.dropna() > 5e-4).mean()),
        "非除息日残差中位": float(resid_on_nondiv.median()) if len(resid_on_nondiv) else float("nan"),
        "corr(|r|,残差)": corr,
        "斜率 g/(1+g)": slope,
        "反解 D/U": g,
        "直读 gap/U": g_direct,
        "年化 日增长率": ann(r_rep),
        "年化 累计比": ann(r_cum),
        "年化 单位比": ann(r_unit),
        "夏普 日增长率": sharpe(r_rep),
        "分红次数(d>0.5%)": ex_days,
    }


if __name__ == "__main__":
    print("=" * 128)
    print("判别「累计净值比率」vs「官方日增长率」谁是权威全收益")
    print(f"  残差 = |累计比 − 官方日增长率|；非除息日 = 累计比与单位比逐位相等的那些天")
    print(f"  结构预测：残差 ≈ |r| · D/U/(1+D/U)，故 corr(|r|, 残差) 应接近 +1")
    print("=" * 128)
    rows = [r for r in (analyse(c, t) for c, t in CASES) if r]
    df = pd.DataFrame(rows)
    show = ["code", "标的", "n", "残差>5e-4 占比", "非除息日残差中位",
            "corr(|r|,残差)", "斜率 g/(1+g)", "反解 D/U", "直读 gap/U"]
    print(df[show].to_string(index=False,
                             formatters={"残差>5e-4 占比": "{:.3f}".format,
                                         "非除息日残差中位": "{:.2e}".format,
                                         "corr(|r|,残差)": "{:+.3f}".format,
                                         "斜率 g/(1+g)": "{:.4f}".format,
                                         "反解 D/U": "{:.3f}".format,
                                         "直读 gap/U": "{:.3f}".format}))
    print()
    print("=" * 128)
    print("三口径年化（同一只、同一区间）")
    print("=" * 128)
    show2 = ["code", "标的", "年化 日增长率", "年化 累计比", "年化 单位比",
             "夏普 日增长率", "分红次数(d>0.5%)"]
    print(df[show2].to_string(index=False,
                              formatters={c: "{:.2%}".format for c in
                                          ("年化 日增长率", "年化 累计比", "年化 单位比")}))
    print()
    print("判读（实测结论，2026-09-18）：")
    print("  ① **非除息日残差中位 = 0**（510880/510300/511010 精确 0，其余 ≈2.5e-5 =")
    print("     日增长率只保留 2 位小数的舍入）⇒ 官方日增长率在非除息日**等于**")
    print("     单位净值增长率 → 排除「日增长率是另一个口径」的怀疑。")
    print("  ② 除息日残差 ≠ 0 且为正 ⇒ 官方日增长率比单位比**多出分红那一块**。")
    print("  ①② 合起来 ⇒ **官方日增长率 = 单位净值增长率 + 分红/前收 = 全收益** ✓")
    print("  ③ 残差只在**分红标的**上出现（512890/159915 全期为 0）⇒")
    print("     累计比 = r/(1+D/U) 是被累计分红稀释的版本 → **不能当全收益**。")
    print("  ⚠️ 不要指望 corr(|r|,残差) ≈ +1：43% 的交易日残差恰为 0（非除息日），")
    print("     零值把相关系数拉低到 0.4~0.7 是正常的，不构成反证。")
    print("  ⚠️「反解 D/U」与「直读 gap/U」只在**累计净值定义诚实**的标的上吻合")
    print("     （515180/515080/563020 三只独立吻合）；510880/510300/511010 的")
    print("     gap 为负、512890/159915 的 gap 含拆分重述 → 这两种情形不可直读。")
