"""行业 + 市值中性化（P2）。

为什么要中性化
--------------
A 股横截面上，**行业**和**市值**两个变量的解释力往往盖过一切量价因子：
不做中性化，一个"高波动因子"可能只是在偷偷做空银行股、做多小盘股。
中性化后剩下的才是因子自身的增量信息。

方法
----
截面 OLS 回归，取残差::

    factor = a + Σ β_i · Industry_i + γ · ln(mktcap) + ε

**不用逐日跑 lstsq** —— 行业哑变量部分等价于"组内去均值"，
连续变量部分再按 FWL（Frisch–Waugh–Lovell）定理做一元回归即可。
两者都可用 groupby.transform 全表向量化，复杂度 O(n)，比逐日 QR 快两个数量级。

数学上这与带完整哑变量集的 OLS 完全等价（FWL 定理保证）。

股本缺失处理
------------
P1 只补齐了 3244 只股票的股本（`total_share`/`float_share`）。
缺失市值的样本退化为**仅行业中性化**（不会因此被丢弃，避免样本大幅缩水）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from aq.factors.vlib import STYLE_FACTORS


def winsorize(s: pd.Series, p: float = 0.01) -> pd.Series:
    """对称截尾：把两端 p 分位以外的极值拉回分位点。"""
    if p <= 0:
        return s
    lo, hi = s.quantile(p), s.quantile(1 - p)
    return s.clip(lower=lo, upper=hi)


def zscore(s: pd.Series) -> pd.Series:
    """截面 z-score（标准差为 0 时返回 0）。"""
    mu, sd = s.mean(), s.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=s.index)
    return (s - mu) / sd


def neutralize(
    df: pd.DataFrame,
    factor_cols: list[str],
    date_col: str = "date",
    industry_col: str = "industry",
    mktcap_col: str = "mktcap",
    suffix: str = "_neu",
    min_industry_size: int = 5,
    winsor: float = 0.01,
    standardize: bool = True,
    inplace: bool = False,
    industry_only: list[str] | None = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """对因子做行业 + 市值中性化，返回带新列的 DataFrame。

    参数
    ----
    min_industry_size
        某交易日某行业样本数少于该值时，并入 ``other``（否则组内去均值会过拟合）。
    winsor
        回归前先截尾，防止极端值主导 β。
    standardize
        残差再做一次截面 z-score，便于后续加权合成。
    verbose
        是否打印"缺市值"提示。**逐日调用时必须设 False**——
        回测打分路径会对 1900+ 个交易日各调一次，否则日志会被刷爆。
    industry_only
        **只做行业中性化、跳过市值中性化**的因子名列表。

        ⚠️ 规模因子（``ln_mktcap``）必须放进来。原因不是工程瑕疵而是数学必然：
        中性化的第二步是「对 ln(mktcap) 做一元回归取残差」，而 ln_mktcap
        对它自己回归的残差**恒等于 0** —— 市值中性化会把规模因子整个抹掉。

        这不是 bug：市值本身就是风格因子，"剔除市值影响后的市值"没有意义。
        同理，若将来加入纯风格因子（如 beta、纯行业暴露），也应放这里。
        默认取 ``vlib.STYLE_FACTORS``，避免调用方忘传导致因子被静默抹平。
    """
    industry_only = set(industry_only) if industry_only is not None else set(STYLE_FACTORS)
    out = df if inplace else df.copy()

    ind = out[industry_col].astype(str)
    # 小行业合并
    cnt = ind.groupby([out[date_col], ind]).transform("size")
    ind = ind.where(cnt >= min_industry_size, "other")
    out["_ind"] = ind

    lncap = np.log(out[mktcap_col].astype("float64").replace(0, np.nan))
    has_cap = lncap.notna()

    n_no_cap = int((~has_cap).sum())
    if n_no_cap and verbose:
        print(f"  [中性化] {n_no_cap:,} 行缺市值 -> 仅做行业中性化")

    grp_keys = [out[date_col], out["_ind"]]

    for col in factor_cols:
        if col not in out.columns:
            continue
        y = out[col].astype("float64")
        # 逐日截尾（截面口径，不能全样本截尾）
        y = y.groupby(out[date_col]).transform(lambda s: winsorize(s, winsor))

        # --- 第一步：行业内去均值（等价于剔除行业哑变量）---
        y_dm = y - y.groupby(grp_keys).transform("mean")

        # --- 第二步：FWL —— 对 ln(mktcap) 做一元回归 ---
        if col in industry_only:
            # 规模因子跳过这一步：ln_mktcap 对 ln(mktcap) 回归的残差恒为 0，
            # 做了等于把它删掉。只保留行业中性化结果。
            resid = y_dm
        else:
            x = lncap.where(has_cap)
            x_dm = x - x.groupby(grp_keys).transform("mean")

            num = (x_dm * y_dm).groupby(out[date_col]).transform("sum")
            den = (x_dm * x_dm).groupby(out[date_col]).transform("sum")
            beta = num / den.replace(0.0, np.nan)

            resid = y_dm - beta * x_dm
            # 缺市值的行：x_dm 为 NaN -> resid 为 NaN，回退成纯行业中性化结果
            resid = resid.where(has_cap, y_dm)
        # 原始因子为 NaN 的行保持 NaN
        resid = resid.where(y.notna())

        if standardize:
            resid = resid.groupby(out[date_col]).transform(zscore)

        out[col + suffix] = resid.astype("float32")

    out.drop(columns=["_ind"], inplace=True)
    return out


def neutralize_single(
    df: pd.DataFrame,
    factor: str,
    **kw,
) -> pd.Series:
    """便捷：中性化单个因子，返回 Series。"""
    r = neutralize(df, [factor], **kw)
    return r[factor + kw.get("suffix", "_neu")]


def industry_stats(df: pd.DataFrame, industry_col: str = "industry") -> pd.DataFrame:
    """行业分布概览（诊断：中性化前先看行业是否过度集中）。"""
    g = df.groupby(industry_col)
    st = pd.DataFrame({
        "n_rows": g.size(),
        "n_symbols": g["symbol"].nunique() if "symbol" in df.columns else g.size(),
    })
    return st.sort_values("n_rows", ascending=False)
