# -*- coding: utf-8 -*-
"""分层收益诊断：判定"打分方向是否搞反了"。

动机
----
A/B 对照回测发现两个权重方案年化都在 -5% 左右，而**等权持有股票池是 +12%**。
-16pp 的差距不可能来自权重分配方式，只可能来自"选股方向"或"组合构建"。

本脚本绕开整套 BacktestEngine，直接用因子面板做最朴素的分层检验：

    每日按综合分排序 -> 取前 20% / 后 20% -> 计算次日收益 -> 累加

若"前 20%"收益显著低于"后 20%"，说明打分方向**整体反了**；
若两者都低于等权基准，说明问题在交易成本或换手，而非方向。

输出三类对照，用于定位问题归属：
1. 等权全池（基准）
2. 打分前 20%（策略想做多的那一端）
3. 打分后 20%（策略想做空/回避的那一端）
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--panel", default="", help="面板 parquet，留空自动找最新的")
    p.add_argument("--weights", default="ic", choices=["ic", "prior", "equal"],
                   help="打分权重来源")
    p.add_argument("--flip", action="store_true",
                   help="把所有因子方向取反（用于验证'方向搞反'假说）")
    p.add_argument("--q", type=float, default=0.2, help="分位比例")
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    return p.parse_args(argv)


def _find_panel() -> Path:
    """选面板文件（按覆盖度而非 mtime；见 scripts/_panel_utils 的说明）。"""
    from scripts._panel_utils import find_panel

    return find_panel(None)


def _stats(daily: pd.Series, label: str) -> dict:
    cum = float((1 + daily).prod() - 1)
    n_yr = len(daily) / 242
    ann = ((1 + cum) ** (1 / n_yr) - 1) if n_yr > 0 else float("nan")
    shp = (daily.mean() / daily.std()) * (242 ** 0.5) if daily.std() else float("nan")
    return {
        "label": label,
        "cum_return": round(cum * 100, 2),
        "annual_return": round(ann * 100, 2),
        "sharpe": round(float(shp), 3),
        "n_days": len(daily),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    panel_path = Path(args.panel) if args.panel else _find_panel()
    print(f"[面板] {panel_path.name}")

    from aq.config.settings import load_settings
    from aq.factors.scoring import build_scorer
    from aq.factors.vlib import spec_names

    cfg = load_settings()
    if args.weights == "ic":
        cfg.model.weight_source = "ic"
        cfg.model.ic_summary_path = str(
            PROJECT_ROOT / "runtime" / "factor_research" / "full_neu" / "summary.csv"
        )
        cfg.model.ic_strict = False
    scorer = build_scorer(cfg)
    w = {k: v for k, v in scorer.weights.items() if abs(v) > 1e-9}
    print(f"[权重] {scorer.ic_source}，{len(w)} 个因子")

    cols = ["date", "symbol", "fwd_ret_1", "t1_tradable"] + list(spec_names())
    df = pd.read_parquet(panel_path)
    keep = [c for c in dict.fromkeys(cols) if c in df.columns]
    df = df[keep]
    if args.start:
        df = df[df["date"] >= args.start]
    if args.end:
        df = df[df["date"] <= args.end]
    print(f"[样本] {len(df):,} 行 / {df['date'].nunique()} 天 / {df['symbol'].nunique()} 只")

    # ---- 用 scorer 的真实逻辑算分（含 library.direction 的定向）----
    factors = [f for f in spec_names() if f in df.columns]
    # 逐因子横截面 z，再按 library.direction 定向
    from aq.factors.library import FactorLibrary

    lib = FactorLibrary()
    zparts = []
    for f in factors:
        d = df.groupby("date")[f]
        mu = d.transform("mean")
        sd = d.transform(lambda s: s.std() or 1.0)
        z = (df[f] - mu) / sd
        flip = lib.direction(f)
        zparts.append(z * flip)
    Z = pd.concat(zparts, axis=1)
    Z.columns = factors

    # 加权求和（权重取绝对值——方向已由 flip 处理）
    scor = sum(Z[f].fillna(0) * abs(w.get(f, 0.0)) for f in factors if f in w)
    if args.flip:
        scor = -scor
        print("[翻转] 已把所有因子方向取反")
    df = df.assign(_score=scor)

    # ---- 分层 ----
    ok = df[df["t1_tradable"].fillna(False)]
    base = _stats(ok.groupby("date")["fwd_ret_1"].mean(), "等权全池（基准）")

    def _quantile_ret(sign: str) -> pd.Series:
        def pick(g: pd.DataFrame) -> float:
            n = max(1, int(len(g) * args.q))
            g2 = g.sort_values("_score", ascending=(sign == "low"))
            return float(g2.head(n)["fwd_ret_1"].mean())
        return ok.groupby("date").apply(pick)

    top = _stats(_quantile_ret("high"), f"打分前 {args.q:.0%}（策略想买）")
    bot = _stats(_quantile_ret("low"), f"打分后 {args.q:.0%}（策略想避）")

    out = pd.DataFrame([base, top, bot])
    print("\n" + "=" * 78)
    print("分层收益诊断（% 为单位，除夏普）")
    print("=" * 78)
    print(out.to_string(index=False))
    print()
    spread = top["annual_return"] - bot["annual_return"]
    print(f"前 20% - 后 20% 年化差: {spread:+.2f}pp")
    if spread < 0:
        print(">>> 分差为负：打分方向疑似整体反了（好的因子在低分端）")
    else:
        print(">>> 分差为正：方向正确")
    gap_base = top["annual_return"] - base["annual_return"]
    print(f"前 20% - 基准 年化差: {gap_base:+.2f}pp")
    if gap_base < 0:
        print(">>> 策略选出的票**跑输等权持有**：问题在选股方向或因子有效性，不是成本")
    else:
        print(">>> 策略选股优于等权：毛收益层面有效，需再看成本与换手")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
