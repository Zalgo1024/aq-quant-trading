# -*- coding: utf-8 -*-
"""从 ic_ts.parquet 完整重建 summary.csv（含 icir_raw / icir_neu 对照列）。

为什么需要这个脚本
------------------
``summary.csv`` 里的 ICIR 是**从 ic_ts.parquet 逐日 IC 时序汇总出来的**，
而 ic_ts.parquet 是不可再生的重产物（要重算就得重跑 1650s 的面板构建）。
所以当 summary.csv 的列被误改坏时，正确的修法不是"打补丁补列"，
而是**从 ic_ts.parquet 重新汇总**——口径由 ``summarize_ic`` 唯一决定，
结果与原始落盘逐位一致。

同时它把 raw/neu 两侧的 ICIR 合并成 icir_raw / icir_neu 两列，
用于判断"某因子的预测力是否主要来自行业/市值暴露"。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from aq.factors.ic import summarize_ic  # noqa: E402
from aq.factors.vlib import spec_names  # noqa: E402

FACTOR_DIR = PROJECT_ROOT / "runtime" / "factor_research"


def _rebuild_one(d: Path, factors: list[str]) -> pd.DataFrame:
    ts = pd.read_parquet(d / "ic_ts.parquet")
    suffix = "_neu" if "_neu" in "".join(ts.columns[:3]) else ""
    cols = [f + suffix for f in factors if f + suffix + "__rankic" in ts.columns]
    sm = summarize_ic(ts, cols, horizon=1)
    sm["factor"] = sm["factor"].str.replace(r"_neu$", "", regex=True)
    return sm


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--raw-dir", default=str(FACTOR_DIR / "full_raw"))
    p.add_argument("--neu-dir", default=str(FACTOR_DIR / "full_neu"))
    p.add_argument("--target", default="neu", choices=["raw", "neu"],
                   help="以哪一侧为基表写回 summary.csv")
    args = p.parse_args(argv)

    raw_d, neu_d = Path(args.raw_dir), Path(args.neu_dir)
    factors = spec_names()

    sm_raw = _rebuild_one(raw_d, factors)
    sm_neu = _rebuild_one(neu_d, factors)
    print(f"[重建] raw {len(sm_raw)} 因子 / neu {len(sm_neu)} 因子")

    a = sm_raw.set_index("factor")["rank_icir"]
    b = sm_neu.set_index("factor")["rank_icir"]

    for d, sm, other, mine, theirs in (
        (raw_d, sm_raw, a, "icir_raw", None),
        (neu_d, sm_neu, b, "icir_neu", a),
    ):
        out = sm.copy()
        if theirs is not None:
            out["icir_raw"] = out["factor"].map(theirs)
        # 回填分位数/衰减等非 ic_ts 派生的列（从现有 csv 里抢救）
        cur = d / "summary.csv"
        if cur.exists():
            old = pd.read_csv(cur)
            nc = "因子" if "因子" in old.columns else "factor"
            old[nc] = old[nc].astype(str).str.replace(r"_neu$", "", regex=True)
            keep = [c for c in old.columns
                    if c not in out.columns and c != nc]
            if keep:
                out = out.merge(old[[nc] + keep].rename(columns={nc: "factor"}),
                                on="factor", how="left")
        order = ["factor", "n_days", "rank_ic", "rank_ic_std", "rank_icir",
                 "icir_raw", "icir_neu",
                 "rank_ic_t", "rank_ic_p", "ic", "ic_std", "icir", "ic_t", "ic_p",
                 "pos_ratio", "direction", "q_ls", "q_mono", "turnover", "autocorr"]
        order += [c for c in out.columns if c.startswith("ic_h")]
        order = [c for c in order if c in out.columns]
        order += [c for c in out.columns if c not in order]
        out = out[order].rename(columns={"factor": "因子"})
        out.to_csv(cur, index=False, encoding="utf-8-sig")
        has = [c for c in ("icir_raw", "icir_neu") if c in out.columns]
        print(f"[写回] {cur.relative_to(PROJECT_ROOT)}  对照列={has}  "
              f"列数={len(out.columns)}")

    # 对照效果汇总
    cmp = pd.DataFrame({"factor": a.index, "icir_raw": a.values})
    cmp["icir_neu"] = cmp["factor"].map(b)
    cmp["neu_ratio"] = (cmp["icir_neu"].abs() / cmp["icir_raw"].abs()).round(3)
    cmp = cmp.sort_values("neu_ratio")
    print("\n" + "=" * 80)
    print("中性化抗性一览（neu_ratio = |ICIR_neu| / |ICIR_raw|）")
    print("  <1 = 中性化后变弱（预测力部分来自风格暴露）")
    print("  >1 = 中性化后更强（成长股/行业暴露曾掩盖真实 alpha）")
    print("=" * 80)
    print(cmp.to_string(index=False))
    weak = cmp[cmp["neu_ratio"] < 0.5]
    print(f"\n[风格依赖] neu_ratio < 0.5 的因子："
          f"{', '.join(weak['factor'].tolist()) if len(weak) else '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
