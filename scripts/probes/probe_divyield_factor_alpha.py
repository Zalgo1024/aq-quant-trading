# -*- coding: utf-8 -*-
"""阶段 B 验收：**ETF 复制检验**（`docs/终局诊断与重启方案.md` §1.3 那套）。

问的是什么
----------
`probe_divyield_decile.py` 已给出：高股息分档**收益端 t≈0、风险端单调改善**。
但"相对全池等权没有超额"还不够 —— 真正致命的问题是 §1.3 那一问：

    高股息策略的收益，能不能被「直接买 红利ETF / 沪深300ETF / 中证1000ETF」免费复制？
    回归残差 alpha 的 t 是多少？

如果 R² 很高、alpha 的 t 很小 ⇒ 这个策略只是三只 ETF 的线性组合，
**没必要自己选股**，阶段 B 判据不通过（与 §1.3 对旧策略层的裁决同一把尺子）。

口径
----
1. 被解释变量：`probe_divyield_decile.collect()` 产出的**调仓期收益**
   （无偏池 = 现役 ∪ 退市；每期 21 交易日，非重叠）。
2. 三因子（全部来自 `EtfNavStore.total_return` 官方日增长率全收益，等权、每日再平衡）：
   - `红利`   ：名称含 红利/股息/高股息（**剔除跨境**：港股红利等）
   - `沪深300`：名称含 沪深300
   - `中证1000`：名称含 中证1000 / 中证 1000
   日收益按调仓期聚合成期收益（复合），与股票侧对齐。
3. 回归：`r_组合 = α + β1·红利 + β2·沪深300 + β3·中证1000 + ε`
   OLS，报 α 的**期均值、年化、t**，以及 β 与 R²。
4. 同时对 **D10−D1 多空**做同一回归 —— 多空才是真正检验 alpha 的对象
   （单边组合会被市场 beta 稀释，多空才是"选股能力"的净值）。

⚠️ 已知偏差（同 `probe_divyield_decile.py`，如实登记）
------------------------------------------------------
未施加"退市可实现损失" ⇒ 高股息档被**高估** ⇒ **alpha 为负或≈0 的结论是稳健的**。

用法
----
    python scripts/probes/probe_divyield_factor_alpha.py
    python scripts/probes/probe_divyield_factor_alpha.py --start 2019-01-01

只读：不写任何数据产物。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve()
ROOT = HERE
while ROOT and not (ROOT / "config" / "base.yaml").exists():
    if ROOT.parent == ROOT:
        break
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))  # 复用 probe_divyield_decile 的面板构造

from aq.data.etf_store import EtfNavStore, load_etf_list  # noqa: E402
from probe_divyield_decile import collect  # noqa: E402

TRADING_DAYS = 252

# 跨境关键词（与 probe_dividend_etf_baseline 同源）：红利因子必须剥掉港股红利
CROSS_BORDER = ("港股", "恒生", "H股", "纳指", "标普", "海外", "QDII", "全球", "亚太")

FACTOR_DEF = {
    "红利": ("红利", "股息", "高股息"),
    "沪深300": ("沪深300", "沪深 300"),
    "中证1000": ("中证1000", "中证 1000"),
}


def build_factors(reb: pd.DatetimeIndex) -> pd.DataFrame:
    """把三只 ETF 因子的日收益聚合成「调仓期收益」。"""
    nav = EtfNavStore()
    lst = load_etf_list()
    name_of = dict(zip(lst["symbol"].astype(str), lst["name"].astype(str)))

    out = {}
    for g, kws in FACTOR_DEF.items():
        codes = []
        for c in nav.available():
            nm = name_of.get(c, name_of.get(c.split(".")[0], ""))
            if any(k in nm for k in kws) and not any(k in nm for k in CROSS_BORDER):
                codes.append(c)
        if not codes:
            print(f"  !! 因子 {g} 无成员")
            continue
        # 日收益等权 → 期收益（复合）
        daily = None
        n_ok = 0
        for c in codes:
            try:
                s = nav.total_return(c)
            except Exception:
                continue
            s = s.dropna()
            if s.empty:
                continue
            daily = s.to_frame() if daily is None else daily.join(s, how="outer")
            n_ok += 1
        if daily is None or n_ok == 0:
            continue
        eq = daily.mean(axis=1).dropna()
        # 调仓期收益：reb[i] → reb[i+1] 之间的复合
        vals = []
        for i in range(len(reb) - 1):
            w = eq.loc[reb[i]:reb[i + 1]]
            w = w.iloc[1:] if len(w) > 1 else w   # 左开右闭：不含起点的当日收益
            vals.append(float((1.0 + w).prod() - 1.0) if len(w) else np.nan)
        out[g] = pd.Series(vals, index=reb[:-1])
        print(f"  因子 {g:<8} {n_ok:>3} 只 ETF")

    return pd.DataFrame(out)


def ols(y: np.ndarray, X: np.ndarray):
    """最小二乘；返回 (系数含截距, t 值含截距, R²)。"""
    n, k = X.shape
    A = np.column_stack([np.ones(n), X])
    beta, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ beta
    dof = n - A.shape[1]
    s2 = float(resid @ resid) / dof if dof > 0 else np.nan
    cov = s2 * np.linalg.pinv(A.T @ A)
    se = np.sqrt(np.diag(cov))
    t = beta / se
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else np.nan
    return beta, t, r2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--rebalance", type=int, default=21)
    ap.add_argument("--min-amount", type=float, default=2e7)
    ap.add_argument("--min-age", type=int, default=180)
    args = ap.parse_args()

    print("=" * 84)
    print("阶段 B 验收：ETF 复制检验（红利 / 沪深300 / 中证1000 三因子）")
    print("=" * 84)

    dy, rt, reb = collect(args.start, args.end, args.rebalance,
                          args.min_amount, args.min_age)

    # 逐期分档（与 probe_divyield_decile 同一口径）
    dec = {i: [] for i in range(1, 11)}
    eq_ret = []
    keep = []
    for d in dy.index:
        row = dy.loc[d].dropna()
        r = rt.loc[d].reindex(row.index).dropna()
        common = row.index.intersection(r.index)
        if len(common) < 100:
            continue
        row, r = row[common], r[common]
        try:
            bins = pd.qcut(row.rank(method="first"), 10, labels=False) + 1
        except ValueError:
            continue
        for i in range(1, 11):
            m = bins == i
            dec[i].append(float(r[m].mean()) if m.any() else np.nan)
        eq_ret.append(float(r.mean()))
        keep.append(d)

    reb_kept = pd.DatetimeIndex(keep)
    print(f"有效调仓期    : {len(reb_kept)} 期")

    print("\n构造 ETF 三因子：")
    F = build_factors(reb_kept)
    if F.shape[1] < 3:
        print("!! 因子不足 3 个")
        return 1
    F = F.dropna()
    idx = F.index
    pos = {d: i for i, d in enumerate(reb_kept)}
    rows = [pos[d] for d in idx]

    af = TRADING_DAYS / args.rebalance  # 期→年的年化因子
    print(f"\n因子期数      : {len(idx)}（{idx[0].date()} ~ {idx[-1].date()}）")
    print(f"年化因子      : {af:.1f}")

    def run(name: str, y_raw: list[float]) -> None:
        y = np.array([y_raw[i] for i in rows], dtype=float)
        m = np.isfinite(y) & np.isfinite(F.values).all(axis=1)
        yy = y[m]
        XX = F.values[m]
        if len(yy) < 20:
            print(f"  {name}: 样本不足")
            return
        beta, t, r2 = ols(yy, XX)
        alpha_p, t_alpha = beta[0], t[0]
        print(f"\n【{name}】n={len(yy)} 期  R²={r2:.3f}")
        print(f"    α（每期）{alpha_p:+.4%}  → 年化 {alpha_p * af:+.2%}   t = {t_alpha:+.2f}")
        for j, g in enumerate(F.columns):
            print(f"    β({g:<8}) = {beta[j + 1]:+.3f}   t = {t[j + 1]:+.2f}")
        verdict = "❌ alpha 不显著（|t| < 2）" if abs(t_alpha) < 2 else "⚠️ alpha 显著"
        print(f"    判据：{verdict}"
              + ("；且 R² 高 ⇒ 收益可被三只 ETF 线性复制，没必要自己选股"
                 if r2 > 0.7 else ""))

    print("\n" + "=" * 84)
    print("回归结果")
    print("=" * 84)

    d10 = dec[10]
    d1 = dec[1]
    run("D10 高股息组合", d10)
    run("D1 低股息组合", d1)
    run("D10 − D1 多空（真正的选股能力）",
        [d10[i] - d1[i] for i in range(len(d10))])
    run("全池等权（对照：应当 α≈0）", eq_ret)

    print("\n" + "-" * 84)
    print("结论口径：")
    print("  1. 因子收益 = 东财官方日增长率全收益（含分红），等权每日再平衡。")
    print("  2. 被解释变量 = 无偏池（现役 ∪ 退市）按 PIT 股息率分档的调仓期收益。")
    print("  3. ⚠️ 未施加退市可实现损失 → 高股息档被高估 ⇒ **α≈0 或为负的结论稳健**。")
    print("  4. 判据照抄 §1.3：R² 高 + α 的 |t| < 2 ⇒ 可被 ETF 免费复制，阶段 B 不通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
