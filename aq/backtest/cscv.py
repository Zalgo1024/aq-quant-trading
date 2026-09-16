# -*- coding: utf-8 -*-
"""防过拟合检验：CSCV / PBO 与 Deflated Sharpe Ratio（DSR）。

为什么必须有这一层
------------------
本项目里所有"看起来最好"的参数——调仓间隔、风控流动性门槛、权重方案、
池子口径——都是**在同一段历史数据上搜出来的**。搜索这个动作本身会给
最优参数的表现注入选择性偏差：哪怕所有参数的真实期望收益都是 0，
挑出来的那个也会显得还行。7.12 已经给出一个强信号：收益对流动性门槛
**非单调**（1e8 → +11.47%，2e7 → −19.30%，0 → +6.29%），这说明
结果对结构参数极度敏感，**在过这一关之前不能声称策略有效**。

两个工具回答两个不同的问题：

1. **PBO（Probability of Backtest Overfitting）**
   问的是："我用历史数据挑参数**这个动作本身**，有多大概率是过拟合？"
   做法（Bailey et al. 2016 的 CSCV）：把样本切成 S 个互不重叠的子期，
   枚举 C(S, S/2) 种"一半训练 / 一半测试"的划分；每次在训练集里挑最优
   参数，再看它在测试集里排第几。若它总是落在后半段，说明"照历史挑"
   这个动作没有预测力 → 过拟合。

2. **DSR（Deflated Sharpe Ratio）**
   问的是："把**试了 N 次**这件事扣掉之后，最优参数的夏普还显著吗？"
   多重检验会让"最好那个"的期望夏普上升到一个门槛 SR0（N 越大越高）；
   DSR = P(观测夏普 > SR0)，本质是"扣掉试错次数后的显著性"。

两者的关系是**互补而非替代**：PBO 检验的是"挑选规则"，DSR 检验的是
"挑出来的那一个"。PBO 低不代表策略能赚钱，DSR 高也不代表参数没被挑过。

参考文献
--------
- Bailey, D. & López de Prado, M. (2012). *The Deflated Sharpe Ratio*.
- Bailey, D., Borwein, J., López de Prado, M. & Zhu, Q. J. (2016).
  *The Probability of Backtest Overfitting*.
- Lo, A. W. (2002). *The Statistics of Sharpe Ratios*.

实现约定（容易踩的坑，都写在各自函数里）
--------------------------------------
- **夏普必须是"每期"的**，不能把年化夏普喂进公式。DSR 的方差估计里
  T 是观测个数（交易日），两者频率必须一致，否则 σ_SR 会小一个
  √252 的量级，DSR 会被系统性高估。
- **峰度用原始峰度**（正态分布 = 3）。pandas 的 ``.kurt()`` 给的是
  **超额**峰度（正态 = 0），传入前必须 +3，否则方差公式里的
  ``(γ2 - 1)/4`` 会在正态分布下退化成 -0.25 而不是 0.5。
"""
from __future__ import annotations

import math
from itertools import combinations
from statistics import NormalDist
from typing import Sequence

import numpy as np
import pandas as pd

# 欧拉-马斯刻若尼常数 γ，出现在"N 次独立试验的最大值期望"的渐近式里
EULER_MASCHERONI = 0.5772156649015329

_ND = NormalDist()


def norm_ppf(p: float) -> float:
    """标准正态的分位函数（用 stdlib，避免引入 scipy 依赖）。"""
    return _ND.inv_cdf(p)


def norm_cdf(x: float) -> float:
    return _ND.cdf(x)


# --------------------------------------------------------------------------
# 基础统计
# --------------------------------------------------------------------------


def sharpe_per_period(returns: Sequence[float]) -> float:
    """每期夏普（均值 / 总体标准差）。

    ⚠️ 不是年化值。喂给 :func:`deflated_sharpe` 前**不要**先乘 √252，
    否则 DSR 会被高估到荒谬的程度（t 统计量凭空放大 15.9 倍）。
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return float("nan")
    sd = float(r.std(ddof=0))
    if sd <= 0:
        return float("nan")
    return float(r.mean() / sd)


def annualize(sr_period: float, periods_per_year: int = 252) -> float:
    return float(sr_period) * math.sqrt(periods_per_year)


def sr_variance(sr_period: float, n_obs: int, skew: float, kurt: float) -> float:
    """夏普估计量的方差（Lo 2002 + Bailey & López de Prado 2012）。

        Var(SR̂) ≈ 1/(T-1) · [ 1 − γ1·SR + ((γ2 − 1)/4)·SR² ]

    其中 γ1 = 偏度，γ2 = **原始**峰度（正态 = 3）。
    代入正态分布（γ1=0, γ2=3）即得经典结果 (1 + SR²/2)/(T-1)。
    """
    if n_obs <= 1 or not np.isfinite(sr_period):
        return float("nan")
    var = (1.0 - skew * sr_period + ((kurt - 1.0) / 4.0) * sr_period ** 2) / (n_obs - 1.0)
    # 极端偏度/峰度下这个二阶近似可能给出负值，钳到一个极小正数是防御性的
    return float(max(var, 1e-18))


# --------------------------------------------------------------------------
# Deflated Sharpe Ratio
# --------------------------------------------------------------------------


def deflated_sharpe(
    returns: Sequence[float],
    n_trials: int = 1,
    periods_per_year: int = 252,
) -> dict:
    """计算 Deflated Sharpe Ratio。

    参数
    ----
    returns
        策略的**每期**收益序列（日频就传日收益）。
    n_trials
        真实试过的独立配置数。⚠️ 这是整个 DSR 里**最需要诚实**的输入：
        只填"最终矩阵里有几个"会系统性低估门槛、高估 DSR。应该把研究
        过程中跑过的所有变体都算进去（不同池子、不同权重方案、不同
        因子组合……），再按相关性打个折（高度相关的配置不算独立试验）。
    periods_per_year
        仅用于把每期夏普换算成年化展示，不参与 DSR 计算。

    返回
    ----
    dict，含 ``sharpe``（每期）、``sharpe_annual``、``sr_threshold``（SR0）、
    ``deflated_sharpe``、``t_stat``、``n_obs``、``skew``、``kurtosis``、
    ``sr_std``（夏普估计量的标准差）。
    """
    s = pd.Series(returns, dtype=float).dropna()
    n = int(len(s))
    if n < 3:
        return {"n_obs": n, "error": "样本不足（< 3 个观测）"}

    sr = sharpe_per_period(s)
    skew = float(s.skew()) if n > 2 else 0.0
    # pandas 给超额峰度（正态=0），公式要原始峰度（正态=3）
    kurt = (float(s.kurt()) if n > 3 else 0.0) + 3.0

    var = sr_variance(sr, n, skew, kurt)
    sigma = math.sqrt(var)

    n_trials = max(1, int(n_trials))
    if n_trials <= 1:
        # N=1 时 Z⁻¹(1-1/N) = Z⁻¹(0) = -∞，公式退化为"不做多重检验校正"
        sr0 = 0.0
    else:
        z1 = norm_ppf(1.0 - 1.0 / n_trials)
        z2 = norm_ppf(1.0 - 1.0 / (n_trials * math.e))
        sr0 = sigma * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)

    dsr = norm_cdf((sr - sr0) / sigma) if sigma > 0 else float("nan")
    # t 统计量：H0 为"真实夏普 = 0"。这个数比 DSR 更好懂 ——
    # 若它本身都不到 2，讨论"扣掉多重检验还剩多少"就没有意义了。
    t_stat = sr * math.sqrt(n)

    return {
        "n_obs": n,
        "sharpe": round(float(sr), 8),
        "sharpe_annual": round(annualize(sr, periods_per_year), 4),
        "annual_return_est": round(float(s.mean()) * periods_per_year, 6),
        "annual_vol_est": round(float(s.std(ddof=0)) * math.sqrt(periods_per_year), 6),
        "skew": round(skew, 4),
        "kurtosis": round(kurt, 4),          # 原始峰度
        "excess_kurtosis": round(kurt - 3.0, 4),
        "sr_std": round(sigma, 8),
        "n_trials": n_trials,
        "sr_threshold": round(float(sr0), 8),
        "sr_threshold_annual": round(annualize(sr0, periods_per_year), 4),
        "t_stat": round(float(t_stat), 4),
        "deflated_sharpe": round(float(dsr), 6),
    }


def dsr_sensitivity(
    returns: Sequence[float],
    trials_grid: Sequence[int] = (1, 5, 10, 25, 50, 100, 200, 500, 1000),
    periods_per_year: int = 252,
) -> list[dict]:
    """对不同的"试了多少次"假设各算一遍 DSR。

    为什么必须给这张表
    ------------------
    ``n_trials`` 无法从数据里估出来，只能靠**诚实地回忆**研究过程。
    既然它是个主观输入，就不该只给一个数字让人自己挑 —— 直接把
    敏感性摊开，读者自己在哪一行取信，结论就是哪一行。
    """
    out = []
    for n in trials_grid:
        d = deflated_sharpe(returns, n_trials=n, periods_per_year=periods_per_year)
        d["n_trials"] = int(n)
        out.append(d)
    return out


# --------------------------------------------------------------------------
# CSCV / PBO
# --------------------------------------------------------------------------


def _sharpe_from_sums(n_obs: float, s1: np.ndarray, s2: np.ndarray) -> np.ndarray:
    """由 (观测数, 一阶和, 平方和) 还原每个配置的夏普。

    用分块累加量而不是原始数据，是为了让 C(S, S/2) 种划分里的每一次
    评估都只要 O(N) 而不是 O(T·N) —— S=16 时是 12870 次划分，
    直接切片重算会慢到没法扫参数。
    """
    mean = s1 / n_obs
    ex2 = s2 / n_obs
    var = np.maximum(ex2 - mean ** 2, 1e-18)   # 总体方差，与 sharpe_per_period 一致
    return mean / np.sqrt(var)


def effective_trials(returns: pd.DataFrame) -> dict:
    """估计"有效独立试验数" N_eff。

    为什么需要它
    ----------
    DSR 里的 ``n_trials`` 必须是**独立**试验次数。但 CSCV 矩阵里的
    配置通常是同一个策略改一个参数来的 —— 它们的日收益高度相关，
    10 个相关配置并不等于 10 次独立尝试。

    用的是相关观测的方差膨胀公式：若 N 个变量的平均两两相关为 ρ̄，
    则均值的方差被放大 (1 + (N-1)·ρ̄) 倍，反推即
        N_eff = N / (1 + (N-1)·ρ̄)

    ⚠️ 这个 N_eff 是**下界**而不是全部：它只覆盖"矩阵里的这几个配置"。
    研究过程中还试过别的池子、别的因子集、别的窗口 —— 那些都不在里面。
    所以报 DSR 时应当把 N_eff 与几个更大的 N 一起摊开看。
    """
    R = returns.dropna(axis=0, how="any")
    n = R.shape[1]
    if n < 2:
        return {"n_configs": n, "n_eff": float(n)}
    c = R.corr().to_numpy(dtype=float)
    off = c[np.triu_indices(n, 1)]
    rho = float(np.mean(off))
    denom = 1.0 + (n - 1) * rho
    n_eff = n / denom if denom > 1e-6 else float(n)
    return {
        "n_configs": int(n),
        "mean_pairwise_corr": round(rho, 4),
        "min_pairwise_corr": round(float(off.min()), 4),
        "max_pairwise_corr": round(float(off.max()), 4),
        "n_eff": round(float(n_eff), 2),
    }


def probability_of_backtest_overfitting(
    returns: pd.DataFrame,
    n_subperiods: int = 16,
    fullsample_best: str | None = None,
) -> dict:
    """CSCV 法估计回测过拟合概率（PBO）。

    参数
    ----
    returns
        ``(T × N)`` 的**样本外**日收益矩阵：行是交易日，列是**各配置**。
        关键要求是每一列都必须是"在别处没被用来挑过它自己"的收益；
        本项目的用法是"每个配置跑一次完整回测、取它的日收益"，
        它们互相之间是同一批参数搜索的**候选**，符合 CSCV 的假设。
    n_subperiods
        切成的子期数 S（必须为偶数且 ≥ 4）。S 越大组合数爆炸
        （S=16 → 12870，S=20 → 184756），但每个子期越短、估计越噪。
        **默认 16**：约 4 个月一段，组合数也够密。
    fullsample_best
        全样本最优配置的列名；不传则按全样本夏普自己挑。

    返回
    ----
    dict，含 ``pbo``、``logit_*`` 分布摘要、``median_is_sharpe``、
    ``median_oos_sharpe``、``haircut``、``prob_oos_negative``、
    ``selected_counts``、以及全样本最优配置的样本外表现分布。
    """
    X = returns.dropna(axis=0, how="any")
    X.columns = [str(c) for c in X.columns]
    if X.shape[1] < 2:
        return {"error": "至少需要 2 个配置才能算 PBO"}
    if X.shape[0] < 8:
        return {"error": f"样本太短（{X.shape[0]} 个观测）"}

    S = int(n_subperiods)
    if S % 2 == 1:
        S -= 1
    S = max(4, S)
    T, N = X.shape
    if T < S * 2:
        return {"error": f"样本 {T} 个观测撑不起 {S} 个子期（每个至少要有 2 个观测）"}

    chunk = T // S
    usable = chunk * S
    # 丢掉**最前面** T%S 个观测：尾部（近期）数据更贴近未来，保留它
    arr = X.to_numpy(dtype=float)[T - usable:]
    blocks = arr.reshape(S, chunk, N)
    sum_s = blocks.sum(axis=1)                    # (S, N)
    sumsq_s = (blocks ** 2).sum(axis=1)           # (S, N)

    tot_n = float(usable)
    tot_sum = sum_s.sum(axis=0)
    tot_sumsq = sumsq_s.sum(axis=0)
    n_half = S // 2

    logits: list[float] = []
    is_sel: list[float] = []
    oos_sel: list[float] = []
    rank_sel: list[int] = []
    sel_counts = {c: 0 for c in X.columns}
    col_pos = {c: i for i, c in enumerate(X.columns)}

    # 全样本最优（用于报告"挑出来的那一个"的样本外分布）
    sr_full = _sharpe_from_sums(tot_n, tot_sum, tot_sumsq)
    best_full = (int(np.argmax(sr_full)) if fullsample_best is None
                 else col_pos[fullsample_best])
    best_full_name = str(X.columns[best_full])
    best_full_oos: list[float] = []

    for is_idx in combinations(range(S), n_half):
        isl = list(is_idx)
        n_in = float(chunk * n_half)
        s_in = sum_s[isl].sum(axis=0)
        ss_in = sumsq_s[isl].sum(axis=0)
        n_out = tot_n - n_in
        s_out = tot_sum - s_in
        ss_out = tot_sumsq - ss_in

        sr_in = _sharpe_from_sums(n_in, s_in, ss_in)
        sr_out = _sharpe_from_sums(n_out, s_out, ss_out)

        b = int(np.argmax(sr_in))
        # 排名：1 = 样本外最好。并列时用 argsort 的次序打断（收益连续，几乎不会并列）
        order = np.argsort(-sr_out, kind="stable")
        ranks = np.empty(N, dtype=int)
        ranks[order] = np.arange(1, N + 1)

        # ⚠️ 相对秩 ρ 必须**越大越好**（ρ=1 是第一名），因为 PBO 定义成
        # P[λ ≤ 0]，而 λ = logit(ρ)。第一版写成 rank/(N+1)（rank=1 最好），
        # 方向整个反过来：真有优势的配置被判成 PBO=1.0，纯噪声反倒
        # 只有 0.88 —— 靠"真信号必须 PBO 低"这条自检才发现。
        rho = (N + 1.0 - ranks[b]) / (N + 1.0)     # 0.5 = 中位数
        logits.append(math.log(rho / (1.0 - rho)))

        is_sel.append(float(sr_in[b]))
        oos_sel.append(float(sr_out[b]))
        rank_sel.append(int(ranks[b]))
        sel_counts[str(X.columns[b])] += 1
        best_full_oos.append(float(sr_out[best_full]))

    lg = np.asarray(logits)
    n_comb = len(lg)
    pbo = float((lg <= 0).mean())

    is_arr = np.asarray(is_sel)
    oos_arr = np.asarray(oos_sel)
    med_is = float(np.median(is_arr))
    med_oos = float(np.median(oos_arr))
    # haircut：照历史挑参带来的"期望缩水"。1.0 = 挑了等于白挑。
    # ⚠️ 只有当样本内**确实有正夏普**时它才有意义：med_is 接近 0 或为负时
    # 这个比值会被放大到几万个百分点（实测出现过 −4328%），纯粹是
    # 除零噪声。所以 med_is ≤ 0 时直接给 None —— "本来就没什么可缩水的"
    # 比一个荒谬的百分比诚实。
    haircut = (1.0 - med_oos / med_is) if med_is > 1e-4 else None

    bfo = np.asarray(best_full_oos)

    return {
        "n_configs": int(N),
        "n_obs": int(usable),
        "n_subperiods": int(S),
        "chunk_size": int(chunk),
        "n_combinations": int(n_comb),
        "pbo": round(pbo, 4),
        "logit_mean": round(float(lg.mean()), 4),
        "logit_median": round(float(np.median(lg)), 4),
        "logit_std": round(float(lg.std(ddof=0)), 4),
        "logit_5pct": round(float(np.percentile(lg, 5)), 4),
        "logit_95pct": round(float(np.percentile(lg, 95)), 4),
        "median_is_sharpe": round(med_is, 6),
        "median_oos_sharpe": round(med_oos, 6),
        "haircut": (round(float(haircut), 4) if haircut is not None else None),
        "prob_oos_negative": round(float((oos_arr < 0).mean()), 4),
        "prob_top1": round(float((np.asarray(rank_sel) == 1).mean()), 4),
        "prob_bottom_half": pbo,
        "selected_counts": {k: int(v) for k, v in
                            sorted(sel_counts.items(), key=lambda kv: -kv[1])},
        "selection_share": {k: round(v / n_comb, 4) for k, v in
                            sorted(sel_counts.items(), key=lambda kv: -kv[1])},
        "fullsample_best": best_full_name,
        "fullsample_best_sharpe": round(float(sr_full[best_full]), 6),
        "best_full_oos_sharpe_median": round(float(np.median(bfo)), 6),
        "best_full_oos_sharpe_mean": round(float(bfo.mean()), 6),
        "best_full_oos_negative_share": round(float((bfo < 0).mean()), 4),
        "best_full_oos_p05": round(float(np.percentile(bfo, 5)), 6),
        "per_config_full_sharpe": {
            str(c): round(float(v), 6) for c, v in zip(X.columns, sr_full)
        },
    }


def interpret_pbo(pbo: float) -> str:
    """给 PBO 一句人话判读（阈值参考 Bailey et al. 2016 的实操口径）。"""
    if not np.isfinite(pbo):
        return "无法判读"
    if pbo >= 0.7:
        return "严重过拟合：按历史挑参几乎没有预测力，必须重做研究设计"
    if pbo >= 0.5:
        return "疑似过拟合：挑出来的参数在样本外经常垫底"
    if pbo >= 0.3:
        return "中等风险：挑选动作有一定信息量，但不稳定"
    if pbo >= 0.1:
        return "较低风险：参数选择大体可复现"
    return "低风险：参数选择在样本外稳定靠前"
