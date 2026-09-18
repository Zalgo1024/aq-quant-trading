# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与 runtime/cscv/ 的既有结果。
# 2026-09-18 从 runtime/ 纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/（见 README「快速开始」第 5 步拉取全市场日线）。
# ---------------------------------------------------------------------------
"""验证「逐日中性化」与「整表中性化」数学等价。

engine 的打分路径改成在已有的 `groupby("date")` 循环里**逐日**调用
`neutralize()`（内存恒定）。这个等价性成立的前提是：
`neutralize()` 内部的每一步本来就按 `date` 分组
（winsorize / 组内去均值 / FWL 求和 / z-score），
且 `min_industry_size` 的样本计数也按 (date, industry) 分组。

所以逐日跑与整表跑应当**数值一致**（只差浮点结合律级别的误差）。
这个测试就是把这个"应当"钉死——否则一旦将来有人往 neutralize 里
加了跨日期的东西（如全样本分位、时序平滑），逐日路径会静默跑偏。

用法::

    python runtime/verify_neu_equivalence.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

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
sys.path.insert(0, str(ROOT))

from aq.factors.library import FactorLibrary        # noqa: E402
from aq.factors.neutralize import neutralize        # noqa: E402

PANEL = ROOT / "data_cache/factors/panel_all_2024-01-02_2024-12-31_b1df4358cb.parquet"
NDATES = 20


def main() -> int:
    if not PANEL.exists():
        print(f"[错误] 缺面板：{PANEL}")
        return 1
    lib = FactorLibrary()
    fac_cols = list(lib.names)
    cols = ["date", "symbol", "industry", "mktcap"] + fac_cols
    df = pq.ParquetFile(PANEL).read(columns=cols).to_pandas()

    dates = sorted(df["date"].unique())[:NDATES]
    slab = df[df["date"].isin(dates)].copy()
    print(f"[样本] {len(slab):,} 行 / {len(dates)} 个交易日 / {len(fac_cols)} 个因子")

    # A) 整表一次性中性化
    whole = slab.copy()
    neutralize(whole, fac_cols, date_col="date", industry_col="industry",
               mktcap_col="mktcap", suffix="_neu", inplace=True, verbose=False)
    A = whole[[c + "_neu" for c in fac_cols]].to_numpy(dtype="float64")

    # B) 逐日中性化（engine 的做法）
    parts = []
    for d in dates:
        g = slab[slab["date"] == d].copy()
        neutralize(g, fac_cols, date_col="date", industry_col="industry",
                   mktcap_col="mktcap", suffix="_neu", inplace=True, verbose=False)
        parts.append(g)
    per = pd.concat(parts).sort_index()
    B = per[[c + "_neu" for c in fac_cols]].to_numpy(dtype="float64")

    assert A.shape == B.shape, f"形状不一致 {A.shape} vs {B.shape}"
    both_nan = np.isnan(A) & np.isnan(B)
    one_nan = np.isnan(A) ^ np.isnan(B)
    diff = np.abs(A - B)
    finite = np.isfinite(diff)

    print(f"[NaN 一致] 两侧同为 NaN 的格点 {both_nan.sum():,}")
    print(f"[NaN 不一致]            {one_nan.sum():,}  <-- 应为 0")
    print(f"[数值差] 最大 {np.nanmax(diff):.3e}  均值 {np.nanmean(diff):.3e}  "
          f"有效格点 {finite.sum():,}")

    ok = one_nan.sum() == 0 and np.nanmax(diff) < 1e-5
    print()
    print("=> 等价 ✅" if ok else "=> ❌ 不等价，逐日路径与整表路径不一致！")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
