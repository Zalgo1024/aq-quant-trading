"""锁定 B1（成本感知定权，``ic_cost_penalty``）的三条不变量。

这个脚本存在的理由：本项目历史上反复被"静默改写口径"坑过
（`--factor-set` 撞缓存、`score_neutralize` 撞缓存、`icir_raw` 当分母…），
而**权重是一切结论的源头，一旦悄悄变了，磁盘上的缓存和已发布结论全部失效
且不会报错**。所以这里把关键不变量写成可执行断言：

  [1] ``cost_penalty == 0`` 必须**逐字不变**地复现历史权威权重。
      这条是硬要求 —— 改坏了它，`liquid_v2 / liquid_neu_val / liquid_neu_main`
      等既有缓存与已发布结论会静默失真。
  [2] ``cost_penalty == 1`` 必须复现文档给出的权威值
      （加权平均换手 0.0732 / 估值三因子权重 36.58%）。
  [3] ``weight_source="ic_wf"`` + ``cost_penalty != 0`` 必须**响亮报错**，
      不允许静默用全样本换手冒充滚动验收（那是前视）。

用法::

    python scripts/check_cost_penalty.py

依赖 ``runtime/factor_research/full_neu_v2/``（summary.csv + corr.csv）；
缺失时**优雅跳过并退出 0**，便于在没有研究产物的环境里跑 CI。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VDIR = ROOT / "runtime" / "factor_research" / "full_neu_v2"
SUMMARY = VDIR / "summary.csv"
CORR = VDIR / "corr.csv"

# cost_penalty=0 的期望权重（改动前的权威复算值，12 个高权重因子）。
# 写成常量而不是"再算一遍对比"，是为了让**任何**对定权路径的改动都被抓住。
EXPECT0 = {
    "vol_ratio": 0.0973, "turnover_chg": 0.0788, "up_down_vol_20": 0.0725,
    "turn_rate_20": 0.0609, "gap": 0.0565, "hl_range_20": 0.0545,
    "rsi_14": 0.0527, "mom_20": 0.0509, "ma_bias_60": 0.0481,
    "intraday_ret": 0.0470, "rev_5": 0.0447, "skew_20": 0.0444,
}
# cost_penalty=1 的期望值（权威函数输出）
EXPECT1_TURNOVER = 0.073207
EXPECT1_VALUE_W = 0.365786


def main() -> int:
    if not (SUMMARY.exists() and CORR.exists()):
        print(f"[跳过] 找不到研究产物 {VDIR}（需要 summary.csv + corr.csv）。")
        print("       先跑 `python scripts/factor_research.py --neutralize` 再回来。")
        return 0

    from aq.factors.library import FactorLibrary
    from aq.factors.scoring import FactorScorer

    rows = list(csv.DictReader(SUMMARY.open(encoding="utf-8-sig")))
    tov = {r.get("因子") or r.get("factor"): float(r["turnover"]) for r in rows}

    def build(penalty: float):
        s = FactorScorer(FactorLibrary())
        s.set_ic_weights(
            SUMMARY, mode="icir", min_abs_ic=0.01, max_p=0.10, cap=0.25, shrink=1.0,
            select=True, corr=CORR, corr_threshold=0.85, min_icir_ratio=0.5,
            cost_penalty=penalty,
        )
        return ({k: v for k, v in s.weights.items() if abs(v) > 1e-9}, s.ic_source)

    w0, src0 = build(0.0)
    w1, src1 = build(1.0)
    ok = True

    print("=" * 68)
    print(f"[信息] penalty=0  ic_source={src0}  因子数={len(w0)}  "
          f"权重和={sum(w0.values()):.8f}")
    print(f"[信息] penalty=1  ic_source={src1}  因子数={len(w1)}  "
          f"权重和={sum(w1.values()):.8f}")
    trn0 = sum(v * tov[k] for k, v in w0.items())
    trn1 = sum(v * tov[k] for k, v in w1.items())
    val0 = sum(w0.get(f, 0.0) for f in ("bp", "ep", "sp"))
    val1 = sum(w1.get(f, 0.0) for f in ("bp", "ep", "sp"))
    print(f"[信息] 加权平均换手   penalty=0 {trn0:.6f}  ->  penalty=1 {trn1:.6f}")
    print(f"[信息] 估值三因子权重 penalty=0 {val0 * 100:.4f}%  ->  "
          f"penalty=1 {val1 * 100:.4f}%")
    print("=" * 68)

    # [1] penalty=0 逐字不变
    worst = max(abs(w0[k] - v) for k, v in EXPECT0.items())
    p1 = worst < 5e-5
    ok &= p1
    print(f"[1] penalty=0 与历史权威权重最大偏差 = {worst:.2e}  "
          f"-> {'PASS 逐字不变' if p1 else 'FAIL 口径被改动了！'}")

    # [2] penalty=1 复现权威值
    p2 = (abs(trn1 - EXPECT1_TURNOVER) < 5e-6
          and abs(val1 - EXPECT1_VALUE_W) < 1e-4)
    ok &= p2
    print(f"[2] penalty=1 复现权威值（换手 {EXPECT1_TURNOVER} / 估值 "
          f"{EXPECT1_VALUE_W * 100:.2f}%）-> {'PASS' if p2 else 'FAIL'}")

    # [3] ic_wf 必须拒绝
    from aq.factors.walkforward import WalkForwardWeighter

    class _M:
        ic_weight_mode = "icir"
        ic_min_abs = 0.01
        ic_max_p = 0.10
        ic_cap = 0.25
        ic_shrink = 1.0
        ic_select = True
        ic_corr_threshold = 0.85
        ic_min_icir_ratio = 0.5
        ic_cost_penalty = 1.0

    class _C:
        model = _M()

    try:
        import pandas as pd

        WalkForwardWeighter(_C(), pd.DataFrame(), verbose=False)
        p3 = False
        print("[3] ic_wf + penalty!=0 -> FAIL 居然没报错（前视风险）")
    except NotImplementedError as exc:
        p3 = True
        print(f"[3] ic_wf + penalty!=0 -> PASS 已主动拒绝：{str(exc).splitlines()[0]}")
    except Exception as exc:  # noqa: BLE001
        p3 = False
        print(f"[3] ic_wf + penalty!=0 -> FAIL 因其它原因报错，门控位置不对："
              f"{type(exc).__name__}: {exc}")
    ok &= p3

    print("=" * 68)
    print(" 结果: " + ("全部通过。" if ok else "存在失败项，请勿据此下结论。"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
