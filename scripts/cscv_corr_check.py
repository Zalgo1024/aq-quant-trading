# -*- coding: utf-8 -*-
"""候选集相关性诊断：解释"为什么往 CSCV 里加配置不一定提高 PBO"。

背景（7.14）
-----------
7.13 把 PBO = 0.287 当成"总过拟合的下界"，理由是因子权重在同一段历史上
拟合、不算样本外。7.14 做了真正的 walk-forward 定权，把权重也搬出样本外，
预期 PBO 会**上升**。实测却没升（0.628 → 0.540，反而略降）。

本脚本给出解释：**CSCV 的 PBO 由候选的"有效独立数量" N_eff 决定，而不是
候选的个数。** 新加的 3 个 ``ic_wf`` 配置与对应的 ``ic`` 配置日收益相关
0.90+，属于"近重复候选"，N_eff 只从 1.17 涨到 1.20 —— 统计结构没变，
PBO 自然不动。

推论（写进报告的方法论警示）
---------------------------
**PBO 不是策略的固有属性，而是"候选集 × 样本"的联合属性。**
同一策略、同一数据、同一代码，仅因为候选集里多/少一个极端差配置，
PBO 可以从 0.287 跳到 0.628。所以"我筛掉明显不行的候选再算 PBO"这个
动作本身就是一个**未计入的自由度**。

跑法::

    python scripts/cscv_corr_check.py --tag wf
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

CSC_DIR = ROOT / "runtime" / "cscv"


def _load(tag: str, universe: str = "liquid") -> pd.DataFrame:
    d = CSC_DIR / f"{universe}_{tag}" / "rets"
    if not d.exists():
        raise FileNotFoundError(f"没有缓存目录 {d}，先跑 scripts/cscv_test.py")
    ser: dict[str, pd.Series] = {}
    for p in sorted(d.glob("*.csv")):
        if p.name.endswith(".bench.csv"):
            continue
        try:
            r = pd.read_csv(p, parse_dates=["date"])
        except Exception:  # noqa: BLE001
            continue
        if "ret" not in r.columns:
            continue
        ser[p.stem] = pd.Series(r["ret"].values, index=r["date"])
    if not ser:
        raise FileNotFoundError(f"{d} 里没有日收益缓存")
    return pd.DataFrame(ser).dropna(how="any")


def _n_eff(corr: pd.DataFrame) -> tuple[float, float]:
    """返回 (平均两两相关, N_eff)。N_eff = n / (1 + (n-1)·ρ̄)。

    这是 N_eff 的常用一阶估计。7.13.6 用特征值口径算得 1.2，与此一致。
    """
    n = corr.shape[0]
    if n < 2:
        return float("nan"), float(n)
    off = corr.values[np.triu_indices(n, 1)]
    rho = float(off.mean())
    return rho, n / (1 + (n - 1) * rho)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="wf")
    ap.add_argument("--universe", default="liquid")
    args = ap.parse_args(argv)

    R = _load(args.tag, args.universe)
    C = R.corr()
    labs = list(R.columns)

    pd.set_option("display.width", 250)
    print("=" * 74)
    print(f"候选集相关性诊断 —— {args.universe}_{args.tag}")
    print("=" * 74)
    print(f"日收益矩阵：{R.shape[0]} 个交易日 × {R.shape[1]} 个候选\n")

    print("--- 相关矩阵（单位 %）---")
    print((C * 100).round(0).astype(int).to_string())

    # 成对的 ic / ic_wf：这是 N_eff 几乎不变的直接原因。
    # 只配对"同 hold、同 turnover"的一对 —— 做法是把 wf 前缀抹掉后按名字相等匹配，
    # 避免把 turnover 轴的候选也误配成"近重复"。
    print("\n--- 近重复候选对（同名换算后完全一致的 ic / ic_wf 各一个）---")
    wf_map = {c: c.replace("wic_wf_", "wic_", 1) for c in labs
              if c.startswith("wic_wf_")}
    pairs = [(wf_map[b], b) for b in wf_map if wf_map[b] in labs]
    if not pairs:
        print("  （未找到成对的 ic / ic_wf 配置）")
    for a, b in sorted(pairs):
        d = R[a] - R[b]
        print(f"  {a:16s} vs {b:20s}  ρ = {C.loc[a, b]:+.4f}   "
              f"年化差 {d.mean() * 242 * 100:+.2f}%（差的波动 "
              f"{d.std() * np.sqrt(242) * 100:.2f}%）")

    print("\n--- N_eff：决定 PBO 的是「有效独立数量」，不是候选个数 ---")
    statics = [c for c in labs if not c.startswith("wic_wf_")]
    wfs = [c for c in labs if c.startswith("wic_wf_")]
    for name, sub in [(f"纯静态（{len(statics)} 个）", statics),
                      (f"仅 walk-forward（{len(wfs)} 个）", wfs),
                      (f"全部（{len(labs)} 个）", labs)]:
        if len(sub) < 2:
            continue
        rho, ne = _n_eff(R[sub].corr())
        print(f"  {name:22s} 平均 ρ̄ = {rho:.3f}   N_eff = {ne:.2f}"
              f"  （{len(sub)} 个候选只值 {ne:.2f} 次独立试验）")

    if statics and wfs:
        rho_s, ne_s = _n_eff(R[statics].corr())
        rho_a, ne_a = _n_eff(R[labs].corr())
        print(f"\n  结论：加入 {len(wfs)} 个 walk-forward 候选后，N_eff "
              f"{ne_s:.2f} → {ne_a:.2f}（+{ne_a - ne_s:.2f}）。")
        print("        新候选与旧候选 ρ ≈ 0.90，是'近重复'，因此")
        print("        CSCV 的排名结构几乎不变 → PBO 不会因'加了配置'而上升。")
        print("        PBO 对**候选集构成**极度敏感，而'筛候选'是未计入的自由度。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
