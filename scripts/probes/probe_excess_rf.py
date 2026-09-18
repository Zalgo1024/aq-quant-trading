# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与 runtime/cscv/ 的既有结果。
# 2026-09-18 从 runtime/ 纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/（见 README「快速开始」第 5 步拉取全市场日线）。
# ---------------------------------------------------------------------------
"""探查：cscv_test.py 的超额口径里，基准收益是否被"双重扣了 rf"。

背景（读 scripts/cscv_test.py 第 549-569 / 618-640 行）：
  556:  rf_daily = args.risk_free / TRADING_DAYS_PER_YEAR
  557:  Rn = R - rf_daily
  569:  R  = Rn                      <-- R 从此已是【扣过 rf】的超额收益
  625:  E  = R.loc[common] - B.loc[common]
                    ^^^^^^^^^^^^^^ 基准 B 是【原始】日收益，没扣 rf

于是 E = (策略 - rf) - 基准 = 策略 - 基准 - rf，
比字面含义"策略 - 基准"多扣了一份 rf（≈2%/年）。
代码自己的打印是"超额口径（策略日收益 − 基准日收益，剥离市场 beta）"——
按这个说法应该是 策略 - 基准。

本探针用**已缓存的**日收益 + bench.csv 直接算两种口径，量化差异。
不重跑回测、不写任何产物。

用法：
  python runtime/probe_excess_rf.py --tag neu_main
  python runtime/probe_excess_rf.py --dir runtime/cscv/liquid_cost1/rets --labels-file ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
sys.path.insert(0, str(ROOT))

from aq.backtest import cscv as C  # noqa: E402
from aq.core.rules import TRADING_DAYS_PER_YEAR  # noqa: E402


def _load_rets(rets_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """读 rets 目录下所有 <label>.csv 与 <label>.bench.csv。"""
    rets: dict[str, pd.Series] = {}
    bench: dict[str, pd.Series] = {}
    for p in sorted(rets_dir.glob("*.csv")):
        if p.name.endswith(".bench.csv") or p.name.endswith(".wf.csv"):
            continue
        label = p.name[:-4]
        r = pd.read_csv(p, parse_dates=["date"])
        rets[label] = pd.Series(r["ret"].values, index=[d.date() for d in r["date"]])
        bp = rets_dir / f"{label}.bench.csv"
        if bp.exists():
            rb = pd.read_csv(bp, parse_dates=["date"])
            bench[label] = pd.Series(rb["ret"].values, index=[d.date() for d in rb["date"]])
    R = pd.DataFrame(rets).dropna(how="any")
    B = pd.DataFrame(bench).dropna(how="any")
    return R, B


def _report(name: str, E: pd.DataFrame, S: int) -> dict:
    r = C.probability_of_backtest_overfitting(E, n_subperiods=S)
    if "error" in r:
        print(f"  {name}: PBO 算不出（{r['error']}）")
        return {}
    bl = r["fullsample_best"]
    dsr = C.deflated_sharpe(E[bl], n_trials=len(E.columns))
    print(f"  {name}")
    print(f"    PBO(S={S}) = {r['pbo']:.4f}   全样本最优 = {bl}")
    print(f"    最优配置（超额口径）年化夏普 = {dsr['sharpe_annual']:+.4f}"
          f"   t = {dsr['t_stat']:+.4f}   DSR(N={dsr['n_trials']}) = {dsr['deflated_sharpe']:.4f}")
    print(f"    各配置超额年化夏普：" +
          "  ".join(f"{c}={C.annualize(C.sharpe_per_period(E[c]), TRADING_DAYS_PER_YEAR):+.3f}"
                    for c in E.columns))
    return {"pbo": r["pbo"], "best": bl, "sharpe_annual": dsr["sharpe_annual"],
            "t": dsr["t_stat"], "dsr": dsr["deflated_sharpe"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="neu_main",
                    help="runtime/cscv/cscv_liquid_<tag>_returns.csv 的 tag 段")
    ap.add_argument("--dir", default="", help="直接给 rets 目录（优先于 --tag）")
    ap.add_argument("--jsons", default="", help="逗号分隔的 json 路径（用来直接列 label）")
    ap.add_argument("--risk-free", type=float, default=0.02)
    ap.add_argument("--S", type=int, default=16)
    args = ap.parse_args()

    if args.dir:
        rets_dir = Path(args.dir)
    else:
        rets_dir = ROOT / "runtime" / "cscv" / f"liquid_{args.tag}" / "rets"
    if not rets_dir.exists():
        print(f"[错误] 找不到 {rets_dir}")
        return 1

    R, B = _load_rets(rets_dir)
    print(f"[数据] 策略矩阵 {R.shape}  基准矩阵 {B.shape}  目录 {rets_dir}")
    if R.empty or B.empty:
        print("[错误] 策略或基准为空")
        return 1

    rf_daily = args.risk_free / TRADING_DAYS_PER_YEAR
    common = R.index.intersection(B.index)
    print(f"[对齐] 共同交易日 {len(common)}"
          f"（{common[0]} ~ {common[-1]}）  rf 日 = {rf_daily:.3e}\n")

    Rc = R.loc[common]
    Bc = B.loc[common]

    # 口径 A：现行代码 —— E = (策略 - rf) - 基准   （双重扣 rf）
    E_A = Rc - rf_daily - Bc
    # 口径 B：字面含义 —— E = 策略 - 基准          （标准超额）
    E_B = Rc - Bc

    print("=" * 92)
    print("口径 A（现行代码：策略 - rf - 基准，基准未扣 rf）")
    print("=" * 92)
    res_A = _report("E_A", E_A, args.S)
    print()
    print("=" * 92)
    print("口径 B（字面：策略 - 基准，两边都不扣 rf）")
    print("=" * 92)
    res_B = _report("E_B", E_B, args.S)

    print()
    print("=" * 92)
    print("差异")
    print("=" * 92)
    if res_A and res_B:
        print(f"  年化夏普：A {res_A['sharpe_annual']:+.4f} → B {res_B['sharpe_annual']:+.4f}"
              f"   （Δ {res_B['sharpe_annual'] - res_A['sharpe_annual']:+.4f}）")
        print(f"  t 统计量：A {res_A['t']:+.4f} → B {res_B['t']:+.4f}"
              f"   （Δ {res_B['t'] - res_A['t']:+.4f}）")
        print(f"  DSR     ：A {res_A['dsr']:.4f} → B {res_B['dsr']:.4f}")
        print(f"  PBO     ：A {res_A['pbo']:.4f} → B {res_B['pbo']:.4f}")
        print(f"  理论差：把 rf 加回一年 ≈ {args.risk_free*100:.2f}% 年化 → 夏普约 +"
              f"{args.risk_free / (pd.concat([Rc, Bc], axis=1).std().mean() * (TRADING_DAYS_PER_YEAR ** 0.5)):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
