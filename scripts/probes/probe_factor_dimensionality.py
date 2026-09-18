# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与 runtime/cscv/ 的既有结果。
# 2026-09-18 从 runtime/ 纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/（见 README「快速开始」第 5 步拉取全市场日线）。
# ---------------------------------------------------------------------------
"""因子池的「有效独立维数」探针（只读，不改任何产物）。

用途：回答「多智能体投票有没有意义」这个前提 ——
若 31 个因子实际只张开 2~3 个独立方向，那么把它们拆给 N 个"角色"再投票，
只是把同一份信息重述 N 遍（N_eff = N/(1+(N-1)rho)）。

方法（每个指标都必须减掉随机基准 —— 本项目方法论要求）：
  1. 参与比 PR = (sum L)^2 / sum L^2   —— 有效独立维数
  2. 熵口径 N_eff = exp(-sum p log p), p = L/sum L
  3. Kaiser: #(lambda > 1)
  4. Marchenko-Pastur 上界 lambda+ = (1 + sqrt(N/T))^2；统计 #(lambda > lambda+)
     T 取**交易日数**而非面板行数：日内截面相关会把 T 虚高，从而高估结构，故保守取日数
  5. 零假设：T×N 独立正态 → 样本相关阵，重复 N_SIM 次，得到每个指标的零分布，
     以及**逐序号**特征值的 95% 分位（哪几个特征值真的超出噪声）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

def _project_root() -> Path:
    """向上找到含 config/base.yaml 的目录作为项目根。

    这样脚本放在 runtime/ 还是 scripts/probes/ 都能跑对。
    """
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "config" / "base.yaml").exists():
            return cand
    return here.parents[1]


ROOT = _project_root()
VARIANT = sys.argv[1] if len(sys.argv) > 1 else "full_neu_v2"
FR = ROOT / "runtime" / "factor_research" / VARIANT
N_SIM = 500
SEED = 20260917


def eig_desc(corr: pd.DataFrame) -> np.ndarray:
    A = (corr.to_numpy(dtype=float) + corr.to_numpy(dtype=float).T) / 2.0
    return np.clip(np.linalg.eigvalsh(A)[::-1], 0.0, None)


def participation_ratio(w: np.ndarray) -> float:
    return float(w.sum() ** 2 / (w ** 2).sum())


def entropy_neff(w: np.ndarray) -> float:
    p = w / w.sum()
    p = p[p > 0]
    return float(np.exp(-(p * np.log(p)).sum()))


def summarize(w: np.ndarray, lam_plus: float) -> dict:
    return {
        "PR": participation_ratio(w),
        "entropy_neff": entropy_neff(w),
        "kaiser_gt1": int((w > 1.0).sum()),
        "above_mp": int((w > lam_plus).sum()),
        "top1_share": float(w[0] / w.sum()),
        "top3_share": float(w[:3].sum() / w.sum()),
        "top5_share": float(w[:5].sum() / w.sum()),
    }


def equicorr_from_pr(pr: float, n: int) -> float:
    """等相关矩阵下，由参与比反解平均两两相关。

    等相关阵特征值 = 1+(n-1)rho（1 个）与 1-rho（n-1 个），sum = n。
    PR = n^2 / [(1+(n-1)rho)^2 + (n-1)(1-rho)^2]；令 a = 1+(n-1)rho 化简得
        a^2 - 2a + [n - n(n-1)/PR] = 0
    → a = 1 + sqrt((n-1)(n/PR - 1))，rho = (a-1)/(n-1)。
    自检：PR=n（全独立）→ rho=0；PR=1（全同一）→ rho=1。
    """
    inner = (n - 1.0) * (n / pr - 1.0)
    if inner <= 0:
        return 0.0
    a = 1.0 + np.sqrt(inner)
    return float(np.clip((a - 1.0) / (n - 1.0), 0.0, 1.0))


def main() -> int:
    corr = pd.read_csv(FR / "corr.csv", index_col=0)
    n = corr.shape[0]
    if corr.shape[1] != n:
        raise SystemExit(f"corr.csv 不是方阵：{corr.shape}")
    if not np.allclose(np.diag(corr.to_numpy()), 1.0):
        print("[警告] 对角线不全是 1，相关阵可能有 NaN 行被填零")

    ic_ts = pd.read_parquet(FR / "ic_ts.parquet")
    date_col = next((c for c in ("date", "trade_date", "dt") if c in ic_ts.columns), None)
    T = int(ic_ts[date_col].nunique()) if date_col else len(ic_ts)
    print(f"变体={VARIANT}  因子数 N={n}  交易日数 T={T}（面板行数={len(ic_ts)}）")

    lam_plus = (1.0 + np.sqrt(n / T)) ** 2
    w = eig_desc(corr)
    obs = summarize(w, lam_plus)

    rng = np.random.default_rng(SEED)
    nulls, sim_w = [], np.zeros((N_SIM, n))
    for s in range(N_SIM):
        C = np.corrcoef(rng.standard_normal((T, n)), rowvar=False)
        wn = np.clip(np.linalg.eigvalsh((C + C.T) / 2.0)[::-1], 0.0, None)
        sim_w[s] = wn
        nulls.append(summarize(wn, lam_plus))
    ndf = pd.DataFrame(nulls)

    print(f"\n=== 全因子池（{n} 个）===")
    print(f"  Marchenko-Pastur 上界 lambda+ = {lam_plus:.4f}")
    print(f"  {'指标':<16}{'实测':>9}{'随机基准':>11}{'随机 5%~95%':>20}{'p(随机>=实测)':>15}")
    for k, label in [
        ("PR", "有效独立维数"),
        ("entropy_neff", "熵口径 N_eff"),
        ("kaiser_gt1", "#(lambda>1)"),
        ("above_mp", "#(lambda>lambda+)"),
        ("top3_share", "前 3 维方差占比"),
    ]:
        o, base = obs[k], ndf[k]
        lo, hi = np.percentile(base, [5, 95])
        print(f"  {label:<16}{o:>9.3f}{base.mean():>11.3f}"
              f"{f'{lo:.3f} ~ {hi:.3f}':>20}{(base >= o).mean():>15.3f}")

    hi95 = np.percentile(sim_w, 95, axis=0)
    print(f"\n  逐序号特征值（列出实测 > 噪声 95% 分位的）：")
    nreal = 0
    for i in range(n):
        if w[i] > hi95[i]:
            nreal += 1
            print(f"    #{i+1:<3} 实测 {w[i]:7.4f}   噪声95% {hi95[i]:7.4f}  超出")
    if nreal == 0:
        print("    （无 —— 全部落在噪声范围内）")
    print(f"  → 超出噪声的特征值个数 = {nreal} / {n}")

    rec_path = FR / "recommended_factors.json"
    if rec_path.exists():
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        raw = rec if isinstance(rec, list) else (
            rec.get("factors") or rec.get("recommended") or rec.get("recommended_factors") or [])
        names = [x if isinstance(x, str) else x.get("name") for x in raw]
        names = [x for x in names if x in corr.columns]
        if names:
            ws = eig_desc(corr.loc[names, names])
            so = summarize(ws, (1.0 + np.sqrt(len(names) / T)) ** 2)
            print(f"\n=== 推荐子集（{len(names)} 个，实际参与 IC 加权打分）===")
            print(f"  有效独立维数 PR = {so['PR']:.3f}   "
                  f"#(lambda>1) = {so['kaiser_gt1']}   前 3 维占 {so['top3_share']*100:.1f}%")
            print(f"  名目 {len(names)} 个因子 → 实际约 {so['PR']:.1f} 个独立方向")
            obs_for_vote = so
        else:
            obs_for_vote = obs
    else:
        obs_for_vote = obs

    print("\n=== 对「多智能体投票」的含义 ===")
    N_sub = len(names) if names else n
    rho_eq = equicorr_from_pr(obs_for_vote["PR"], N_sub)
    print(f"  由 PR 反解等效平均两两相关 rho_eq = {rho_eq:.3f}"
          f"（N={N_sub}，等相关近似）")
    for nn in (3, 5, 6):
        neff = nn / (1 + (nn - 1) * rho_eq)
        print(f"    {nn} 个角色 -> 有效独立票数 N_eff = {neff:.2f} 票")
    print("  ⚠️ 这是**下限**：角色若各自聚合整张面板，会一起落进最大的那 2~3 个维度，")
    print("     实际 rho 高于 rho_eq、N_eff 更低（对照组：6 个 CSCV 配置实测 rho=0.908 → 1.08 票）。")

    # ---- 前几个主成分由哪些因子主导 ----
    print("\n=== 前 6 个主成分的载荷（各列 |loading| 最大的 5 个因子）===")
    A = corr.to_numpy(dtype=float)
    wv, V = np.linalg.eigh((A + A.T) / 2.0)
    order = np.argsort(wv)[::-1]
    cols = list(corr.columns)
    for k in range(min(6, n)):
        v = V[:, order[k]]
        idx = np.argsort(np.abs(v))[::-1][:5]
        share = wv[order[k]] / wv.sum()
        parts = ", ".join(f"{cols[i]}{v[i]:+.2f}" for i in idx)
        print(f"  PC{k+1}  lambda={wv[order[k]]:6.3f}  方差占比 {share*100:5.1f}%  |  {parts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
