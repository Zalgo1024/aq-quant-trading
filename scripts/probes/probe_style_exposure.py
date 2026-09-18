# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与 runtime/cscv/ 的既有结果。
# 2026-09-18 从 runtime/ 纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/（见 README「快速开始」第 5 步拉取全市场日线）。
# ---------------------------------------------------------------------------
"""风格暴露探针（只读产物，不改任何东西）。

动机：按年分段检验发现，所有配置的超额收益符号在**同一年完全一致**
（2019/2020 全负，2021/2022/2023 全正，2024~2026 全负）。
这不是选股能力，这是一个**共同的时序暴露**。最可能的候选是**规模/风格**：
liquid 池（全市场）的平均市值远小于沪深300，即便打分已做市值中性化，
持有 top20 相对沪深300 仍会留下系统性的小盘暴露。

检验：
  1) 单因子： r_p = a + b * r_300
  2) 双因子： r_p = a + b_mkt * r_300 + b_size * (r_1000 - r_300)
     若 b_size 显著而 a ≈ 0 → 所谓 alpha 其实是一个规模押注。
  3) 换基准：分别用 000300 / 000905 / 000852 / 000016 做基准重算年度超额与符号检验。

所有 t 值用 Newey-West（HAC）修正 —— 日频残差有自相关，OLS 的 t 会虚高。
"""
from __future__ import annotations

import math
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
CSC = ROOT / "runtime" / "cscv"
IDX = ROOT / "data_cache" / "index_bars"
PPY = 242
RF = 0.02

NAMES = {"000300": "沪深300", "000905": "中证500", "000852": "中证1000", "000016": "上证50"}


def load_index(code: str) -> pd.Series:
    p = IDX / f"{code}.SH.parquet"
    df = pd.read_parquet(p)
    cols = {c.lower(): c for c in df.columns}
    dc = next((cols[k] for k in ("date", "trade_date", "dt") if k in cols), None)
    if dc is None:
        dc = df.index.name or df.columns[0]
        idx = pd.to_datetime(df[dc])
    else:
        idx = pd.to_datetime(df[dc])
    df = df.set_index(idx)
    # 收盘价列
    cc = next((cols[k] for k in ("close", "close_price", "收盘") if k in cols), None)
    if cc is None:
        num = df.select_dtypes("number")
        cc = num.columns[0]
    s = pd.to_numeric(df[cc], errors="coerce").sort_index()
    return s.pct_change()


def nw_ols(y: np.ndarray, X: np.ndarray):
    """OLS + Newey-West(HAC) 标准误。返回 (beta, t, r2, n)"""
    n, k = X.shape
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ (X.T @ y)
    e = y - X @ beta
    u = X * e[:, None]
    L = int(4 * (n / 100.0) ** (2.0 / 9.0))
    S = u.T @ u
    for l in range(1, L + 1):
        w = 1.0 - l / (L + 1.0)
        G = u[l:].T @ u[:-l]
        S += w * (G + G.T)
    V = XtX_inv @ S @ XtX_inv
    se = np.sqrt(np.diag(V))
    r2 = 1.0 - (e @ e) / ((y - y.mean()) @ (y - y.mean()))
    return beta, beta / se, r2, n, L


def binom_p_ge(k: int, n: int) -> float:
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def ann_sharpe(d: pd.Series) -> float:
    return float(d.mean() / d.std(ddof=1) * math.sqrt(PPY)) if d.std(ddof=1) else float("nan")


def run(tag: str, dirname: str, only_best: bool = True) -> None:
    rdir = CSC / dirname / "rets"
    if not rdir.exists():
        print(f"[缺] {rdir}")
        return
    js_p = CSC / f"cscv_{dirname}.json"
    best = None
    if js_p.exists():
        import json
        js = json.loads(js_p.read_text(encoding="utf-8"))
        best = (js.get("pbo_main") or {}).get("fullsample_best") or \
               (js.get("pbo_excess") or {}).get("fullsample_best")

    idx_ret = {}
    for c in NAMES:
        try:
            idx_ret[c] = load_index(c)
        except Exception as exc:  # noqa: BLE001
            print(f"  [警告] 指数 {c} 载入失败：{exc}")

    for p in sorted(rdir.glob("*.csv")):
        if p.name.endswith(".bench.csv"):
            continue
        label = p.stem
        if only_best and best and label != best:
            continue
        r = pd.read_csv(p, index_col=0, parse_dates=True).iloc[:, 0]
        r = r.dropna()
        print("\n" + "=" * 100)
        print(f"[{tag}] {label}" + ("   ← 全样本最优配置" if label == best else ""))
        print(f"  区间 {r.index[0].date()} ~ {r.index[-1].date()}   {len(r)} 天")

        # ---------- 回归 ----------
        common = r.index
        for c in ("000300", "000852"):
            if c in idx_ret:
                common = common.intersection(idx_ret[c].dropna().index)
        y = r.loc[common].to_numpy()
        mkt = idx_ret["000300"].loc[common].to_numpy()
        if "000852" in idx_ret:
            size = (idx_ret["000852"].loc[common] - idx_ret["000300"].loc[common]).to_numpy()

        X1 = np.column_stack([np.ones(len(y)), mkt])
        b1, t1, r21, n1, L1 = nw_ols(y, X1)
        print(f"\n  单因子  r_p = a + b·沪深300     R²={r21:.3f}  (NW 滞后 L={L1})")
        print(f"    alpha    = {b1[0]*PPY*100:7.2f}%/年   t = {t1[0]:6.2f}")
        print(f"    beta_300 = {b1[1]:7.3f}              t = {t1[1]:6.2f}")

        if "000852" in idx_ret:
            X2 = np.column_stack([np.ones(len(y)), mkt, size])
            b2, t2, r22, n2, L2 = nw_ols(y, X2)
            print(f"\n  双因子  r_p = a + b_mkt·沪深300 + b_size·(中证1000−沪深300)"
                  f"     R²={r22:.3f}")
            print(f"    alpha     = {b2[0]*PPY*100:7.2f}%/年   t = {t2[0]:6.2f}   ← 关键")
            print(f"    beta_mkt  = {b2[1]:7.3f}              t = {t2[1]:6.2f}")
            print(f"    beta_size = {b2[2]:7.3f}              t = {t2[2]:6.2f}   ← 关键")
            if abs(t2[2]) > 2 and abs(t2[0]) < 2:
                print("    ⚠️ 判读：规模暴露显著、alpha 不显著 → "
                      "所谓 alpha 主要是**小盘押注**，不是选股能力。")

        # ---------- 换基准的年度超额 + 符号检验 ----------
        print(f"\n  换基准后的年度超额（策略 − 基准，两边都不扣 rf）")
        hdr = f"    {'年':<6}" + "".join(f"{NAMES[c]:>12}" for c in idx_ret)
        print(hdr)
        years = sorted(set(r.index.year))
        tally = {c: 0 for c in idx_ret}
        for yy in years:
            m = r.index.year == yy
            row = f"    {yy:<6}"
            for c, s in idx_ret.items():
                b = s.loc[r.index[m]].dropna()
                rr = r.loc[r.index[m]].reindex(b.index).dropna()
                b = b.reindex(rr.index)
                if len(rr) < 30:
                    row += f"{'-':>12}"
                    continue
                exc = (1 + rr).prod() - (1 + b).prod()
                if exc > 0:
                    tally[c] += 1
                row += f"{exc*100:>11.1f}%"
            print(row)
        print(f"    {'正年数':<6}" + "".join(f"{tally[c]:>11d}/{len(years)}" for c in idx_ret))
        print(f"    {'二项p':<6}" + "".join(
            f"{binom_p_ge(tally[c], len(years)):>12.3f}" for c in idx_ret))
        print("    ⚠️ 相邻年自相关 → 上式 p 为乐观估计。")


def main() -> int:
    targets = sys.argv[1:] or ["neu_main", "cost1"]
    for t in targets:
        run(t, f"liquid_{t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
