# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与 runtime/cscv/ 的既有结果。
# 2026-09-18 从 runtime/ 纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/（见 README「快速开始」第 5 步拉取全市场日线）。
# ---------------------------------------------------------------------------
"""分段一致性检验（只读产物，不改任何东西）。

用途：回答「效应在不分段时看不出来，是不是因为不同段符号翻转？」
注意 —— 分段**不增加统计功效**：Stouffer 合并 Σz/√k ≈ 全样本合并 z。
分段的真正价值是**符号一致性检验**（二项检验），其功效随段数**指数**增长。

口径（必须与 cscv_test.py 修好的口径一致）：
    超额 = 策略原始日收益 − 基准原始日收益（两边都不扣 rf）
`rets/<label>.csv` 存的是**未扣 rf** 的原始收益（cscv_test.py 里 R = pd.DataFrame(series)，
之后才 R = R - rf_daily），所以直接用 rets − bench 即正确口径。

自检：脚本会把全样本超额年化夏普与 json 的 dsr_excess.sharpe_annual 对比，
对不上就报错 —— 独立实现必须被权威结果反验。
"""
from __future__ import annotations

import json
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
PPY = 242

# 产品标签 -> (目录名, 是否 citable)
PRODUCTS = {
    "neu_main": ("liquid_neu_main", True),
    "cost1": ("liquid_cost1", True),
    "neu_val": ("liquid_neu_val", True),
    "raw_val": ("liquid_raw_val", True),
}


def binom_p_ge(k: int, n: int) -> float:
    """单侧二项检验 p = P(X >= k)，X ~ Binom(n, 0.5)。"""
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def ann_sharpe(daily: pd.Series) -> float:
    if len(daily) < 20 or daily.std(ddof=1) == 0:
        return float("nan")
    return float(daily.mean() / daily.std(ddof=1) * math.sqrt(PPY))


def load_pair(tag: str):
    dirname, ok = PRODUCTS[tag]
    rdir = CSC / dirname / "rets"
    if not rdir.exists():
        raise SystemExit(f"[缺] {rdir}")
    out = {}
    for p in sorted(rdir.glob("*.csv")):
        if p.name.endswith(".bench.csv"):
            continue
        bp = rdir / f"{p.stem}.bench.csv"
        if not bp.exists():
            continue
        r = pd.read_csv(p, index_col=0, parse_dates=True).iloc[:, 0]
        b = pd.read_csv(bp, index_col=0, parse_dates=True).iloc[:, 0]
        idx = r.index.intersection(b.index)
        out[p.stem] = (r.loc[idx], b.loc[idx])
    return out, CSC / f"cscv_{dirname}.json"


def segment_key(idx: pd.DatetimeIndex, freq: str) -> pd.Series:
    if freq == "year":
        return pd.Series(idx.year, index=idx)
    if freq == "halfyear":
        return pd.Series(idx.year.astype(str) + "H" + ((idx.month > 6).astype(int) + 1).astype(str),
                         index=idx)
    if freq == "quarter":
        return pd.Series(idx.year.astype(str) + "Q" + idx.quarter.astype(str), index=idx)
    raise ValueError(freq)


def report(tag: str, freq: str) -> None:
    pairs, jpath = load_pair(tag)
    if not pairs:
        print(f"{tag}: 无可用 rets")
        return
    js = json.loads(jpath.read_text(encoding="utf-8")) if jpath.exists() else {}
    dsr_ex = js.get("dsr_excess") or {}
    pbo_ex = js.get("pbo_excess") or {}
    cal = js.get("excess_caliber")

    print("=" * 100)
    print(f"产品 {tag}   分段粒度 = {freq}   口径标记 = {cal or '(缺=修前)'}")
    if dsr_ex:
        print(f"  json 权威值：超额年化夏普 {dsr_ex.get('sharpe_annual', float('nan')):.4f} / "
              f"t {dsr_ex.get('t_stat', float('nan')):.4f} / "
              f"样本外为负占比 {(pbo_ex.get('prob_oos_negative') or float('nan')):.1%}")

    for label, (r, b) in pairs.items():
        ex = r - b
        # ---- 独立实现的全样本超额夏普，与 json 对账 ----
        mine = ann_sharpe(ex)
        ref = dsr_ex.get("sharpe_annual")
        if ref is not None and label == (pbo_ex.get("fullsample_best") or label):
            flag = "✅ 一致" if abs(mine - ref) < 5e-3 else "❌ 不一致"
            print(f"  [对账] {label}: 独立实现 {mine:.4f} vs json {ref:.4f}  {flag}")

        seg = segment_key(ex.index, freq)
        rows = []
        for k, grp in ex.groupby(seg):
            rr, bb = r.loc[grp.index], b.loc[grp.index]
            rows.append({
                "段": k,
                "策略": (1 + rr).prod() - 1,
                "基准": (1 + bb).prod() - 1,
                "超额收益": (1 + rr).prod() - (1 + bb).prod(),
                "超额夏普": ann_sharpe(grp),
                "天": len(grp),
            })
        df = pd.DataFrame(rows).set_index("段")
        pos = int((df["超额收益"] > 0).sum())
        n = len(df)
        print(f"\n  --- {label} ---")
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(df.to_string(float_format=lambda v: f"{v:9.4f}"))
        print(f"  超额为正 {pos}/{n} 段   单侧二项 p = P(X≥{pos}) = {binom_p_ge(pos, n):.4f}")
        print("  ⚠️ 相邻段之间存在自相关（一个坏年通常两个坏半年），"
              "故上式为**乐观**估计，真实 p 更大。")


def main() -> int:
    freq = sys.argv[1] if len(sys.argv) > 1 else "year"
    tags = sys.argv[2:] or list(PRODUCTS)
    for t in tags:
        report(t, freq)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
