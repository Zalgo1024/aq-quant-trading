# -*- coding: utf-8 -*-
"""用中性化因子做分层诊断（修正版）。

为什么需要"修正版"
------------------
初版 ``quantile_diag.py`` 直接用 RAW 因子 + ``fwd_ret_1`` 的**分组平均收益**
判断方向，得出"几乎所有因子高值端都更好"的结论，与实际 IC 全面矛盾。

根因是**分组平均收益混入了时序效应**：
- 高波动股在大盘上涨日涨更多（beta），这些日子拉高了其平均收益；
- 但同一天内横向比较，高波动股多数时候略跑输（这才是 alpha 方向，IC 为负）。

因此判断"横截面选股方向"必须用**逐日横截面**口径，不能用分组平均收益。

本脚本做两件事：
1. ``--method ic``：算**中性化因子**的逐日横截面 RankIC（与因子研究一致的口径），
   这是判断方向唯一可靠的依据。
2. ``--method quantile``：算中性化后的分组收益，但**同时输出逐日均值（已剔除
   时序效应）与累计均值**两种口径，让两者的差异显现出来，避免再被误导。
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

from aq.factors.neutralize import neutralize  # noqa: E402
from aq.factors.vlib import SPEC_BY_NAME, spec_names  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--panel", default="")
    p.add_argument("--method", choices=["ic", "quantile", "both"], default="both")
    p.add_argument("--n-q", type=int, default=5)
    p.add_argument("--min-cross", type=int, default=50)
    p.add_argument("--no-neutralize", action="store_true",
                   help="不中性化（对比用；结果会被风格暴露污染）")
    return p.parse_args(argv)


def _find_panel() -> Path:
    """选面板文件（按覆盖度而非 mtime；见 scripts/_panel_utils 的说明）。

    历史教训：原按 mtime 取"最新"，实际选到了 261 只的小池，
    用它验证因子方向等于在有偏样本上做结论。
    """
    from scripts._panel_utils import find_panel

    return find_panel(None)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    panel_path = Path(args.panel) if args.panel else _find_panel()
    print(f"[面板] {panel_path.name}")

    df = pd.read_parquet(panel_path)
    factors = [f for f in spec_names() if f in df.columns]
    df = df[df["t1_tradable"].fillna(False)]
    print(f"[样本] {len(df):,} 行 / {df['date'].nunique()} 天")

    if not args.no_neutralize:
        print("[中性化] 行业 + 对数市值 ...")
        df = neutralize(df, factors, date_col="date",
                        industry_col="industry", mktcap_col="mktcap")
        use = [f + "_neu" for f in factors]
    else:
        print("[中性化] 已跳过（结果会混入风格暴露）")
        use = factors

    ret = "fwd_ret_1"

    if args.method in ("ic", "both"):
        print("\n" + "=" * 84)
        print("口径 A：逐日横截面 RankIC（判断方向的唯一可靠依据）")
        print("=" * 84)
        print(f"{'因子':22s} {'库方向':>7s} {'RankIC':>9s} {'NW-t':>8s} {'n天':>6s} {'一致':>5s}")
        print("-" * 84)
        rows = []
        for f in factors:
            col = f + "_neu" if not args.no_neutralize else f
            d = df[["date", col, ret]].dropna()
            ics = []
            for _, g in d.groupby("date"):
                if len(g) < args.min_cross:
                    continue
                ic, _ = stats.spearmanr(g[col], g[ret])
                if np.isfinite(ic):
                    ics.append(ic)
            if len(ics) < 20:
                continue
            a = np.asarray(ics)
            m = a.mean()
            t = m / (a.std(ddof=1) / np.sqrt(len(a)))
            rows.append((f, SPEC_BY_NAME[f].direction, m, t, len(a)))
        for f, cur, m, t, n in sorted(rows, key=lambda x: x[2]):
            should = 1 if m > 0 else -1
            ok = "Y" if should == cur else "N <=="
            print(f"{f:22s} {cur:+7d} {m:+9.4f} {t:+8.2f} {n:6d} {ok:>5s}")
        bad = [r[0] for r in rows if (1 if r[2] > 0 else -1) != r[1]]
        print(f"\n汇总：{len(rows)} 个因子；方向与 IC 不一致 {len(bad)} 个"
              f"{'：' + ', '.join(bad) if bad else '（全部一致）'}")

    if args.method in ("quantile", "both"):
        print("\n" + "=" * 84)
        print(f"口径 B：{args.n_q} 档收益（★ 注意：均值口径会混入时序效应）")
        print("=" * 84)
        print("  逐日均值 = 每天先算档内均值再跨日平均（剔除时序效应，可信）")
        print("  累计均值 = 全样本直接平均（被大盘涨跌主导，仅参考）")
        print()
        hdr = "".join(f"{'q' + str(i):>10s}" for i in range(args.n_q))
        print(f"{'因子':22s}{hdr}{'qTop-qBot(逐日)':>16s}")
        print("-" * 84)
        for f in factors:
            col = f + "_neu" if not args.no_neutralize else f
            d = df[["date", col, ret]].dropna()
            if len(d) < 5000:
                continue
            d = d.copy()
            d["_q"] = d.groupby("date")[col].transform(
                lambda s: pd.qcut(s.rank(method="first"), args.n_q, labels=False)
                if s.nunique() >= args.n_q else np.nan
            )
            # 逐日：先按 (date, q) 聚合，再对 date 平均
            per_day = d.dropna(subset=["_q"]).groupby(["date", "_q"])[ret].mean()
            by_q = per_day.groupby("_q").mean()
            if len(by_q) < args.n_q:
                continue
            # 方向调整：让 q0 = "按 direction 最该买"，便于横向比较
            if SPEC_BY_NAME[f].direction < 0:
                by_q = by_q.iloc[::-1]
            cells = "".join(f"{by_q.iloc[i]*100:>10.4f}" for i in range(args.n_q))
            spread = (by_q.iloc[-1] - by_q.iloc[0]) * 242 * 100
            print(f"{f:22s}{cells}{spread:>16.1f}")
        print("\n  表头 q0 = 按库方向最该买的一端，qTop = 同左；spread 为 qTop-qBot 年化(%)")
        print("  若 direction 正确，各档应大致单调递减（q0 最高）")


if __name__ == "__main__":
    main()
