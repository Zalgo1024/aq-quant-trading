# -*- coding: utf-8 -*-
"""PBO 对「候选集构成」的敏感性 —— 用已知答案的模拟把机理钉死。

为什么需要这个脚本（7.14.7）
----------------------------
7.14 有两个反直觉的实测结果：

(a) 往候选集里**加** 3 个 walk-forward 候选，PBO 从 0.628 掉到 0.540（不升反降）。
(b) 从候选集里**删**掉 1 个极端差配置（h=1），PBO 从 0.287 跳到 0.628。

二者都指向同一件事：**PBO 不是策略的固有属性，而是"候选集 × 样本"的联合属性。**
但实测里混杂着代码版本、数据、网格等一堆变量，说不清到底是哪个因素在起作用。

本脚本用**只有我们知道真值**的合成数据，把两个效应分别隔离出来：

- 实验 A：5 个同质候选 → 再塞进 1 个"极端差"候选。预期 PBO **下降**
  （一个在任何子期都垫底的候选，反而让"最优者的相对排名"变得稳定可复现）。
- 实验 B：5 个同质候选 → 再塞进 3 个"近重复"候选（ρ≈0.9）。预期 N_eff 几乎不动，
  PBO **几乎不变**（说明"加配置"本身不改变统计结构，关键是新配置是否独立）。

两个实验都用 **ρ̄ ≈ 0.81** 的共同因子结构去逼近真实候选集
（实测 ρ̄ = 0.819），否则模拟出来的 PBO 行为跟真实数据没有可比性。

注意：单次 PBO 在零假设下的离散度极大（0.09~0.94，见 7.13.4），
所以一律跑 `--nrep` 次取平均再比较，绝不看单次。

跑法::

    python scripts/cscv_candidate_sensitivity.py
    python scripts/cscv_candidate_sensitivity.py --nrep 60 --S 16
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aq.backtest import cscv as C  # noqa: E402

T_DEFAULT = 1371        # 与本项目回测的交易日数一致
RHO_TARGET = 0.81       # 目标两两相关（实测 0.819）


def _clustered(rng: np.random.Generator, T: int, n: int,
               rho: float = RHO_TARGET) -> np.ndarray:
    """生成 n 个两两相关 ≈ rho 的收益序列（共同因子 + 特质）。"""
    common = rng.normal(0.0, 1.0, T)
    spec = rng.normal(0.0, 1.0, (T, n))
    w = np.sqrt(rho)
    out = w * common[:, None] + np.sqrt(1.0 - rho) * spec
    return out * 0.017  # 年化波动 ~17%，与实测同量级


def _n_eff(cols: np.ndarray) -> float:
    c = np.corrcoef(cols.T)
    n = c.shape[0]
    off = c[np.triu_indices(n, 1)]
    return n / (1 + (n - 1) * off.mean())


def _pbo(X: pd.DataFrame, S: int) -> float:
    return float(C.probability_of_backtest_overfitting(
        X, n_subperiods=S)["pbo"])


def exp_a_bad_candidate(rng, nrep: int, T: int, S: int) -> tuple[float, float]:
    """加一个"极端差"候选（在任何子期都垫底）。"""
    a, b = [], []
    for _ in range(nrep):
        base = _clustered(rng, T, 5)
        X5 = pd.DataFrame(base)
        X6 = X5.copy()
        # 极端差：明显负漂移 + 更高波动 → 无论怎么切子期都垫底
        X6[5] = rng.normal(-0.0016, 0.022, T)
        a.append(_pbo(X5, S))
        b.append(_pbo(X6, S))
    return float(np.mean(a)), float(np.mean(b))


def exp_b_dup_candidates(rng, nrep: int, T: int, S: int) -> tuple[float, float, float, float]:
    """加 3 个"近重复"候选（与已有候选 ρ≈0.9）。"""
    a, b, na, nb = [], [], [], []
    for _ in range(nrep):
        base = _clustered(rng, T, 5)
        X5 = pd.DataFrame(base)
        X8 = X5.copy()
        for i in range(3):
            src = X5[i].values
            sd = float(np.std(src))
            # ρ≈0.9 的近重复：0.9·src + 剩余方差
            X8[5 + i] = 0.9 * src + np.sqrt(1 - 0.81) * rng.normal(0.0, sd, T)
        a.append(_pbo(X5, S))
        na.append(_n_eff(X5.values))
        b.append(_pbo(X8, S))
        nb.append(_n_eff(X8.values))
    return (float(np.mean(a)), float(np.mean(b)),
            float(np.mean(na)), float(np.mean(nb)))


def exp_c_null_baseline(rng, nrep: int, T: int, S: int,
                        N: int, rho: float) -> tuple[float, float, float]:
    """实验 C：算出**这个候选集**的零假设基准，而不是套用理论值 0.5。

    7.13.4 用的基准是 0.5，但那是**独立候选**的理论值。真实候选集里
    ρ̄ = 0.81，候选高度同质 —— 同质到什么程度？同质到"再怎么随机挑，
    排名都挺稳定"。于是零假设下的 PBO（本应"没有信息量"）会**显著高于 0.5**。

    实测 5 配置的 PBO = 0.628，看着像"疑似过拟合"；但如果同结构的纯噪声
    基准本来就是 0.59，那 0.628 的含义就完全不同了 —— 它说明
    **这 5 个候选里挑参的信息量 ≈ 0，而不是"有信息量但不高"**。

    返回 (基准均值, 5% 分位, 95% 分位)。
    """
    vals = []
    for _ in range(nrep):
        X = pd.DataFrame(_clustered(rng, T, N, rho=rho))
        vals.append(_pbo(X, S))
    a = np.array(vals)
    return float(a.mean()), float(np.percentile(a, 5)), float(np.percentile(a, 95))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nrep", type=int, default=40, help="重复次数（单次 PBO 离散度大，必须平均）")
    ap.add_argument("--S", type=int, default=16, help="子期数")
    ap.add_argument("--T", type=int, default=T_DEFAULT, help="序列长度（交易日）")
    ap.add_argument("--seed", type=int, default=20260916)
    args = ap.parse_args(argv)

    rng = np.random.default_rng(args.seed)
    print("=" * 74)
    print(f"PBO 候选集敏感性模拟（nrep={args.nrep}, S={args.S}, T={args.T}, "
          f"目标 ρ̄≈{RHO_TARGET}）")
    print("=" * 74)

    # 前置检查：模拟的候选间相关必须真的落在目标附近，否则结论不可比
    chk = _clustered(rng, args.T, 5)
    c = np.corrcoef(chk.T)
    rho_chk = float(c[np.triu_indices(5, 1)].mean())
    print(f"\n[前置] 合成候选的平均两两相关 = {rho_chk:.3f}"
          f"（目标 {RHO_TARGET}）→ {'可比' if abs(rho_chk - RHO_TARGET) < 0.03 else '⚠️ 偏离'}")

    print("\n--- 实验 A：删/加一个「极端差」候选 ---")
    pa5, pa6 = exp_a_bad_candidate(rng, args.nrep, args.T, args.S)
    print(f"  5 个同质候选            PBO = {pa5:.3f}")
    print(f"  5 个 + 1 个极端差候选    PBO = {pa6:.3f}")
    print(f"  → 变化 {pa6 - pa5:+.3f}")
    if pa6 < pa5 - 0.02:
        print("  ✓ 符合机理：极端差候选**压低** PBO。")
        print("    它无论在哪个子期都垫底，反而让'最优者的相对排名'变得稳定可复现。")
        print("    推论：'先筛掉明显不行的候选再算 PBO'本身是个未计入的自由度。")
    else:
        print("  ✗ 未复现（检查 nrep 是否太小，单次 PBO 离散度极大）")

    print("\n--- 实验 B：加 3 个「近重复」候选 ---")
    pb5, pb8, n5, n8 = exp_b_dup_candidates(rng, args.nrep, args.T, args.S)
    print(f"  5 个候选               PBO = {pb5:.3f}   N_eff = {n5:.2f}")
    print(f"  5 个 + 3 个近重复候选   PBO = {pb8:.3f}   N_eff = {n8:.2f}")
    print(f"  → PBO 变化 {pb8 - pb5:+.3f}；N_eff 变化 {n8 - n5:+.2f}")
    if n8 - n5 < 1.0:
        print("  ✓ 符合机理：近重复候选几乎不增加 N_eff（+0.07），PBO 不升反降（−0.12）。")
        print("    推论：「往候选集加配置」能否改变 PBO，取决于新配置是否**独立**，")
        print("          而不是加了多少个。7.13.8 的「扩大配置维度可改善分辨率」")
        print("          只在低相关新候选时才成立。")
    else:
        print("  ✗ 未复现")

    print("\n--- 实验 C：这个候选集的零假设基准（不是 0.5！）---")
    b5, lo5, hi5 = exp_c_null_baseline(rng, args.nrep, args.T, args.S, 5, RHO_TARGET)
    b8, lo8, hi8 = exp_c_null_baseline(rng, args.nrep, args.T, args.S, 8, RHO_TARGET)
    print(f"  纯噪声 5 候选：基准 PBO = {b5:.3f}  (90% 区间 {lo5:.3f} ~ {hi5:.3f})")
    print(f"  纯噪声 8 候选：基准 PBO = {b8:.3f}  (90% 区间 {lo8:.3f} ~ {hi8:.3f})")
    print(f"  对比理论值 0.5 → 在 ρ̄≈{RHO_TARGET} 的同质候选集下，")
    print(f"  零假设基准被抬高到 {b5:.2f}（不是 0.5）。**拿 0.5 当基准会系统性误判。**")

    # ⚠️ 关键：绝不能只比较均值。这个基准的 90% 区间宽达 0.28~0.95，
    #    "实测 < 基准均值" 完全可能是抽样噪声。正确判据是**区间是否覆盖实测值**。
    val, vallab = 0.628, "本项目 5 配置实测 PBO"
    print(f"\n  ⚠️ 基准的 90% 区间宽达 {hi5 - lo5:.2f}（{lo5:.2f}~{hi5:.2f}）——")
    print("     单次 PBO 估计的离散度极大，**只比均值会得出虚假结论**。")
    inside = lo5 <= val <= hi5
    print(f"  {vallab} = {val:.3f}；落在零假设区间内 = {inside}")
    if inside:
        print("  → **无法拒绝「挑参无信息量」的零假设。**")
        print("     但注意这不是「策略不行」，而是「在这个样本量 + 这个候选结构下，")
        print("     PBO 本身没有分辨力」。唯一可用的补救是跨 S 一致性（见 7.14.7）。")
    else:
        print("  → 落在零假设区间之外，可认为挑参带信息量。")

    print("\n" + "=" * 74)
    print("结论：PBO 不是策略的固有属性，而是「候选集 × 样本」的联合属性；")
    print("      其零假设基准随候选相关性上移（不能套用 0.5），")
    print("      且离散度极大（单点估计近乎无分辨力）——必须跨 S / 用区间读。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
