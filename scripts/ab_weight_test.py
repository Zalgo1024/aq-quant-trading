# -*- coding: utf-8 -*-
"""P2 A/B 对照回测：先验权重 vs IC 自动定权。

用途
----
验证 ``set_ic_weights``（筛选 + min-max 归一化 + 幂次压缩 + 相关性去冗余）
是否真的优于手工先验权重。**这是 P2 的验收口径之一**：

    若 IC 加权不能击败先验权重，说明"用实测 IC 定权"这件事在本因子集上
    没有增量价值，应当退回先验权重而不是自我说服。

跑法
----
    E:/Python/python.exe scripts/ab_weight_test.py
    E:/Python/python.exe scripts/ab_weight_test.py --start 2021-01-01 --end 2026-08-31

输出
----
- 终端对比表
- ``runtime/ab_weight_test/ab_result.json``  机器可读结论
- ``runtime/ab_weight_test/ab_report.md``    人读报告片段
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

FACTOR_DIR = PROJECT_ROOT / "runtime" / "factor_research"
OUT_DIR = PROJECT_ROOT / "runtime" / "ab_weight_test"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P2 权重方案 A/B 对照回测")
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--end", default="2026-08-31")
    p.add_argument("--capital", type=float, default=1_000_000.0)
    p.add_argument("--icir-ratio", type=float, default=0.5,
                   help="中性化抗性门控：|ICIR_neu|/|ICIR_raw| 低于此值剔除")
    p.add_argument("--corr-threshold", type=float, default=0.85)
    p.add_argument("--shrink", type=float, default=1.0,
                   help="幂次压缩 exponent；1.0 = 线性")
    p.add_argument("--cap", type=float, default=0.25)
    p.add_argument("--variant", default="full_neu",
                   help="用哪份因子研究结果定权（目录名）")
    return p.parse_args(argv)


def _latest_variant_dir(name: str) -> Path | None:
    d = FACTOR_DIR / name
    return d if (d / "summary.csv").exists() else None


def _run_one(cfg, label: str, **over) -> dict:
    """跑一次回测，返回指标字典。"""
    from aq.backtest.engine import BacktestEngine

    for k, v in over.items():
        setattr(cfg.model, k, v)
    t0 = time.time()
    res = BacktestEngine(cfg).run()
    el = time.time() - t0

    m = res.metrics
    get = lambda k, d=None: getattr(m, k, d)  # noqa: E731

    return {
        "label": label,
        "total_return": get("total_return"),
        "annual_return": get("annual_return"),
        "sharpe": get("sharpe"),
        "max_drawdown": get("max_drawdown"),
        "calmar": get("calmar"),
        "win_rate": get("win_rate"),
        "n_trades": len(res.trades),
        "elapsed_sec": round(el, 1),
        "weight_source": cfg.model.weight_source,
        "ic_weight_mode": cfg.model.ic_weight_mode,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    vdir = _latest_variant_dir(args.variant)
    if vdir is None:
        print(f"[错误] 找不到因子研究结果目录 runtime/factor_research/{args.variant}/summary.csv")
        print("       请先跑 scripts/factor_research.py --neutralize")
        return 1
    summary_csv = vdir / "summary.csv"
    corr_csv = vdir / "corr.csv"
    print(f"[数据] 定权依据: {summary_csv.relative_to(PROJECT_ROOT)}")
    print(f"[数据] 相关性矩阵: {'有' if corr_csv.exists() else '无'}")

    from aq.config.settings import load_settings

    cfg = load_settings()
    cfg.backtest.start = args.start
    cfg.backtest.end = args.end
    cfg.backtest.initial_cash = args.capital

    # 先看看 IC 加权会选出哪些因子、权重怎么分布（不跑回测，秒出）
    from aq.factors.scoring import build_scorer

    cfg.model.weight_source = "ic"
    cfg.model.ic_summary_path = str(summary_csv)
    cfg.model.ic_weight_mode = "icir"
    cfg.model.ic_select = True
    cfg.model.ic_corr_threshold = args.corr_threshold
    scorer = build_scorer(cfg)
    print(f"\n[IC 定权] 来源={scorer.ic_source}，有权重的因子 {len(scorer.active_factors())} 个")
    w = {k: round(v, 4) for k, v in scorer.weights.items() if abs(v) > 1e-9}
    for k, v in sorted(w.items(), key=lambda x: -abs(x[1])):
        print(f"    {k:22s} {v:+.4f}")
    print(f"    Σ|w| = {sum(abs(x) for x in w.values()):.4f}")
    n_active = len(scorer.active_factors())

    # ---- B：IC 自动定权 ----
    print("\n" + "=" * 118)
    print("开始 A/B 对照回测")
    print("=" * 118)
    res_b = _run_one(cfg, "B_ic_weight")

    # ---- A：先验权重（同区间）----
    cfg.model.weight_source = "prior"
    res_a = _run_one(cfg, "A_prior_weight")

    rows = [res_a, res_b]
    df = pd.DataFrame(rows)
    show = df[["label", "total_return", "annual_return", "sharpe",
               "max_drawdown", "calmar", "win_rate", "n_trades", "elapsed_sec"]].copy()
    for c in ("total_return", "annual_return", "max_drawdown"):
        if show[c].dtype.kind == "f":
            show[c] = (show[c] * 100).round(2)

    print("\n" + "=" * 118)
    print(f"A/B 对照结果（{args.start} ~ {args.end}）")
    print("=" * 118)
    print("  收益/回撤单位：%")
    print(show.to_string(index=False))

    def _f(v):
        try:
            return float(v)
        except Exception:  # noqa: BLE001
            return float("nan")

    d_ret = _f(res_b["total_return"]) - _f(res_a["total_return"])
    d_shp = _f(res_b["sharpe"]) - _f(res_a["sharpe"])
    d_mdd = _f(res_b["max_drawdown"]) - _f(res_a["max_drawdown"])

    verdict = []
    verdict.append(f"收益差(B-A): {d_ret*100:+.2f}pp")
    verdict.append(f"夏普差(B-A): {d_shp:+.3f}")
    verdict.append(f"回撤差(B-A): {d_mdd*100:+.2f}pp（正=更深回撤=更差）")
    print("\n" + " | ".join(verdict))
    better = d_shp > 0 and d_ret > 0
    print(f"\n结论：IC 加权{'优于' if better else '未优于'}先验权重"
          f"（以 收益&夏普 双升为准）")

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": args.start, "end": args.end},
        "capital": args.capital,
        "weight_basis": str(vdir.relative_to(PROJECT_ROOT)),
        "ic_factors": scorer.active_factors(),
        "n_ic_factors": n_active,
        "ic_weights": {k: round(v, 6) for k, v in w.items()},
        "A_prior": res_a,
        "B_ic": res_b,
        "delta": {"total_return_pp": round(d_ret * 100, 3),
                  "sharpe": round(d_shp, 4),
                  "max_drawdown_pp": round(d_mdd * 100, 3)},
        "ic_better": bool(better),
    }
    (OUT_DIR / "ab_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n[落盘] {OUT_DIR / 'ab_result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
