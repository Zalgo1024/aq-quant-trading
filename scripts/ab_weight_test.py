# -*- coding: utf-8 -*-
"""P2 A/B 对照回测：先验权重 vs IC 自动定权。

用途
----
验证 ``set_ic_weights``（筛选 + min-max 归一化 + 幂次压缩 + 相关性去冗余）
是否真的优于手工先验权重。**这是 P2 的验收口径之一**：

    若 IC 加权不能击败先验权重，说明"用实测 IC 定权"这件事在本因子集上
    没有增量价值，应当退回先验权重而不是自我说服。

池子口径（``--universe``）
-------------------------
- ``hs300``（默认）：沪深300 成分池，**可交易实盘口径**。
  注意它带**幸存者偏差**（拿不到历史成分，见 ``aq/data/universe.py``），
  且池子大小随 ``asof`` 漂移（越早越小），故**跨起点不可比**。
- ``liquid``：全市场流动性动态池（上市满 180 天 + 非 ST + 近 20 日日均
  成交额 ≥ 2e7），**因子有效性研究口径**，与 ``factor_research`` 一致。
  无成分依赖，不受幸存者偏差影响。

跑法
----
    python scripts/ab_weight_test.py                          # hs300 池
    python scripts/ab_weight_test.py --universe liquid         # 全市场池
    python scripts/ab_weight_test.py --start 2021-01-01 --end 2026-08-31

输出
----
- 终端对比表
- ``runtime/ab_weight_test/ab_result.json``  机器可读结论
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
    p.add_argument("--universe", default="hs300", choices=["hs300", "liquid", "all"],
                   help="池子口径：hs300=可交易实盘池(含幸存者偏差)；"
                        "liquid/all=全市场流动性池(研究口径)")
    p.add_argument("--tag", default="",
                   help="结果文件后缀标记，避免不同口径互相覆盖")
    p.add_argument("--rebalance-days", type=int, default=None,
                   help="调仓间隔（交易日）。不传则用配置里的 backtest.freq "
                        "（默认 1d = 每日调仓）")
    p.add_argument("--hold-scan", action="store_true",
                   help="调仓频率扫描：对 --hold-days 里每个间隔各跑一组 A/B，"
                        "用于找出成本与信号衰减之间的最优点")
    p.add_argument("--hold-days", default="1,5,10,20",
                   help="--hold-scan 的间隔列表（交易日），逗号分隔")
    p.add_argument("--force-panel", action="store_true",
                   help="强制重建因子面板缓存。补过股本/行情等底层数据后必须加，"
                        "否则面板一直吃旧缓存，改了数据也看不出变化")
    p.add_argument("--risk-turnover", type=float, default=None,
                   help="覆盖 risk.liquidity_min_turnover（单日成交额门槛）。"
                        "用于实测该门槛对仓位的压制程度，默认沿用配置")
    p.add_argument("--scan-risk", default="",
                   help="对 --risk-turnover 的多个取值各跑一遍（逗号分隔，单位元）。"
                        "例如 1e8,2e7,0 —— 与 --hold-scan 不能同时用")
    p.add_argument("--weights", default="both",
                   choices=["both", "prior", "ic"],
                   help="跑哪些权重方案。--hold-scan 时建议用 ic（更省时间）")
    return p.parse_args(argv)


def _latest_variant_dir(name: str) -> Path | None:
    d = FACTOR_DIR / name
    return d if (d / "summary.csv").exists() else None


def _apply_universe(cfg, universe: str) -> str:
    """把池子口径写进 cfg，返回人类可读说明。

    两种口径的实现路径不同（这是两套面板语义差异的根源）：

    - ``hs300``：走 ``UniverseSelector`` 的指数池 —— 引擎侧
      ``PanelConfig(universe="all", symbols=...)`` 会在**引擎决定池子后**
      跳过流动性过滤。
    - ``liquid``：让 ``UniverseSelector`` 不设指数（=全市场），并把流动性
      门槛交给它在**引擎侧**独立完成，使其语义与
      ``PanelConfig(universe="liquid")`` 对齐（上市满 180 天 + 非 ST +
      近 20 日日均成交额 ≥ 2e7）。
    """
    if universe == "hs300":
        cfg.universe.index = "hs300"
        return "沪深300 成分池（可交易实盘口径；含幸存者偏差，跨起点不可比）"

    # 全市场流动性池：与 aq.factors.panel.PanelConfig(universe="liquid") 对齐
    cfg.universe.index = "all"
    cfg.universe.min_list_days = 180          # 对齐 PanelConfig.min_list_days
    cfg.universe.min_turnover = 2e7           # 对齐 PanelConfig.min_amount
    cfg.universe.lookback = 20
    cfg.universe.max_symbols = 0              # 0 = 不截断（避免按代码序取前 N 的偏置）
    return ("全市场流动性池（研究口径；上市满 180 天 + 非 ST + "
            "近 20 日日均成交额 ≥ 2e7，不限规模）")


def _f(v):
    """尽力转 float，失败给 nan（用于比较与格式化）。"""
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return float("nan")


def _run_one(cfg, label: str, **over) -> dict:
    """跑一次回测，返回指标字典。"""
    from aq.backtest.engine import BacktestEngine

    for k, v in over.items():
        if k.startswith("bt_"):
            # bt_* 前缀 → 写进 cfg.backtest（如 bt_rebalance_days）
            setattr(cfg.backtest, k[3:], v)
        else:
            setattr(cfg.model, k, v)
    t0 = time.time()
    res = BacktestEngine(cfg).run()
    el = time.time() - t0

    m = res.metrics
    get = lambda k, d=None: getattr(m, k, d)  # noqa: E731

    row = {
        "label": label,
        "total_return": get("total_return"),
        "annual_return": get("annual_return"),
        "sharpe": get("sharpe"),
        "max_drawdown": get("max_drawdown"),
        "calmar": get("calmar"),
        "win_rate": get("win_rate"),
        # 基准相关（接入沪深300 后为真实值；无基准时为 None）
        "alpha": get("alpha"),
        "beta": get("beta"),
        "information_ratio": get("information_ratio"),
        "n_trades": len(res.trades),
        "elapsed_sec": round(el, 1),
        "weight_source": cfg.model.weight_source,
        "ic_weight_mode": cfg.model.ic_weight_mode,
        "universe": cfg.universe.index,
        "rebalance_days": getattr(cfg.backtest, "rebalance_days", None),
    }
    # 仓位诊断：低仓位会把 beta 和收益一起压低，必须和绩效并列输出，
    # 否则很容易把"没买进去"误读成"选股不行"。
    diag = getattr(res, "diagnostics", None) or {}
    for k in ("avg_exposure", "median_exposure", "days_over_90pct",
              "n_days", "avg_holdings", "max_holdings"):
        row[k] = diag.get(k)
    return row


def _hold_scan(cfg, args, hold_days: list[int], out_path=None) -> list[dict]:
    """调仓频率扫描：对每个间隔各跑一组，输出成本/信号衰减的权衡表。

    为什么这一步是关键
    ------------------
    研究口径（``attribution_test.py --hold-scan``）已证明日频调仓的成本
    拖累高达 18.43pp/年，10 日调仓最优。但那是**逐日累乘的近似模型**；
    引擎这边的多日调仓能力**刚接进来**，必须在完整撮合 + 真实成本下重验。

    为什么增量落盘
    --------------
    单次要跑 5~20 分钟，四个间隔加起来半小时以上。**每跑完一组就写盘**，
    中途被打断也不至于全部重来（2026-09-16 就遇到过：前三组白跑了，
    日志停在 [4/4] 但没有任何结果文件）。
    """
    rows: list[dict] = []
    modes = (["prior", "ic"] if args.weights == "both" else [args.weights])
    total = len(hold_days) * len(modes)
    n = 0
    for h in hold_days:
        for src in modes:
            n += 1
            cfg.model.weight_source = src
            label = f"h={h:>2d}d_{src}"
            print(f"\n[{n}/{total}] 调仓间隔 {h} 交易日 | 权重 {src}")
            r = _run_one(cfg, label, bt_rebalance_days=h)
            # 面板只在**第一次**强制重建：--force-panel 是"底层数据变了"的信号，
            # 不是"每次回测都要重算"。重算一次后缓存已是最新的，
            # 后面三次照常复用即可（否则扫描 N 个间隔就白重建 N 次面板）。
            if getattr(cfg.backtest, "panel_force", False):
                cfg.backtest.panel_force = False
            r["hold_days"] = h
            rows.append(r)
            if out_path is not None:
                # 增量落盘：先写部分结果，中断也留得下
                try:
                    out_path.write_text(
                        json.dumps({"partial": True,
                                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                                    "universe": args.universe,
                                    "mode": "hold_scan",
                                    "hold_days": hold_days,
                                    "rows": rows},
                                   ensure_ascii=False, indent=2),
                        encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    print(f"       （增量落盘失败：{e}）")
    return rows


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
    cfg.backtest.panel_force = bool(getattr(args, "force_panel", False))

    # 池子口径
    uni_note = _apply_universe(cfg, args.universe)
    print(f"\n[池子] {args.universe} —— {uni_note}")

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

    # ---- 风控流动性门槛扫描 ----
    # 为什么要扫这个：股票池按"20 日日均成交额 ≥ 2e7"选股，风控却按
    # "单日成交额 ≥ 1e8"卡单 —— 两道门槛差 5 倍且语义不同，20 只目标里
    # 平均只有 5 只买得进，仓位被压在 27%。扫一遍就能把这条因果链钉死。
    if args.scan_risk:
        vals = [float(x) for x in str(args.scan_risk).split(",") if x.strip()]
        h = args.rebalance_days or 10
        rows = []
        for v in vals:
            cfg.risk.liquidity_min_turnover = v
            print(f"\n[风险门槛] 单日成交额 ≥ {v/1e8:.2f} 亿 | 调仓间隔 {h} 日")
            r = _run_one(cfg, f"turnover>={v:g}", bt_rebalance_days=h)
            r["risk_turnover"] = v
            rows.append(r)

        df = pd.DataFrame(rows)
        cols = ["risk_turnover", "total_return", "sharpe", "max_drawdown",
                "alpha", "beta", "information_ratio", "n_trades"]
        for c in ("avg_exposure", "avg_holdings"):
            if c in df.columns and df[c].notna().any():
                cols.append(c)
        if "rejected_orders" in df.columns:
            cols.append("rejected_orders")
        show = df[cols].copy()
        for c in ("total_return", "max_drawdown", "alpha", "avg_exposure"):
            if c in show.columns and show[c].dtype.kind == "f":
                show[c] = (show[c] * 100).round(2)
        show["risk_turnover"] = (show["risk_turnover"] / 1e8).round(3)
        print("\n" + "=" * 110)
        print(f"风控流动性门槛扫描（{args.start} ~ {args.end} | 池子 {args.universe}"
              f" | 调仓间隔 {h} 日）")
        print("=" * 110)
        print("  risk_turnover 单位：亿元；收益/alpha/仓位 单位：%")
        print(show.to_string(index=False))

        out = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "period": {"start": args.start, "end": args.end},
            "universe": args.universe,
            "benchmark": getattr(cfg.backtest, "benchmark", None),
            "weight_basis": str(vdir.relative_to(PROJECT_ROOT)),
            "mode": "risk_scan",
            "rebalance_days": h,
            "rows": rows,
        }
        suffix = f"_{args.tag}" if args.tag else ""
        p = OUT_DIR / f"ab_riskscan_{args.universe}{suffix}.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[落盘] {p}")
        return 0

    # ---- 调仓频率扫描模式 ----
    if args.hold_scan:
        hold_days = [int(x) for x in str(args.hold_days).split(",") if x.strip()]
        hold_days = sorted({h for h in hold_days if h >= 1})
        suffix = f"_{args.tag}" if args.tag else ""
        scan_path = OUT_DIR / f"ab_holdscan_{args.universe}{suffix}.json"
        rows = _hold_scan(cfg, args, hold_days, out_path=scan_path)

        df = pd.DataFrame(rows)
        cols = ["hold_days", "weight_source", "total_return", "annual_return",
                "sharpe", "max_drawdown", "alpha", "beta",
                "information_ratio", "n_trades"]
        # 仓位诊断列：有才有（老引擎跑出来的结果没有）
        if "avg_exposure" in df.columns and df["avg_exposure"].notna().any():
            cols += ["avg_exposure", "avg_holdings", "days_over_90pct"]
        cols.append("elapsed_sec")
        show = df[cols].copy()
        for c in ("total_return", "annual_return", "max_drawdown", "alpha", "avg_exposure"):
            if c in show.columns and show[c].dtype.kind == "f":
                show[c] = (show[c] * 100).round(2)

        print("\n" + "=" * 118)
        print(f"调仓频率扫描（{args.start} ~ {args.end} | 池子 {args.universe}）")
        print("=" * 118)
        print("  收益/alpha/仓位 单位：%；alpha 为年化超额")
        print(show.to_string(index=False))

        # 最优间隔：以夏普为主口径（收益与风险兼顾），并列最大回撤
        for src in sorted({r["weight_source"] for r in rows}):
            sub = [r for r in rows if r["weight_source"] == src]
            best = max(sub, key=lambda r: _f(r["sharpe"]))
            print(f"\n[{src}] 最优调仓间隔 = {best['hold_days']} 交易日"
                  f"（夏普 {_f(best['sharpe']):.3f}、收益 "
                  f"{_f(best['total_return'])*100:.2f}%、回撤 "
                  f"{_f(best['max_drawdown'])*100:.2f}%）")

        out = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "period": {"start": args.start, "end": args.end},
            "capital": args.capital,
            "universe": args.universe,
            "universe_note": uni_note,
            "benchmark": getattr(cfg.backtest, "benchmark", None),
            "weight_basis": str(vdir.relative_to(PROJECT_ROOT)),
            "mode": "hold_scan",
            "hold_days": hold_days,
            "rows": rows,
        }
        out_path = scan_path
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[落盘] {out_path}")
        return 0

    # ---- 单次 A/B：B IC 自动定权 / A 先验权重 ----
    print("\n" + "=" * 118)
    print("开始 A/B 对照回测")
    print("=" * 118)
    over = {}
    if args.rebalance_days:
        over["bt_rebalance_days"] = args.rebalance_days
        print(f"[调仓] 间隔 {args.rebalance_days} 交易日"
              f"（覆盖配置里的 backtest.freq）")

    res_b = _run_one(cfg, "B_ic_weight", **over)

    cfg.model.weight_source = "prior"
    res_a = _run_one(cfg, "A_prior_weight", **over)

    rows = [res_a, res_b]
    df = pd.DataFrame(rows)
    show = df[["label", "total_return", "annual_return", "sharpe", "max_drawdown",
               "calmar", "alpha", "beta", "information_ratio",
               "n_trades", "elapsed_sec"]].copy()
    for c in ("total_return", "annual_return", "max_drawdown"):
        if show[c].dtype.kind == "f":
            show[c] = (show[c] * 100).round(2)

    print("\n" + "=" * 118)
    print(f"A/B 对照结果（{args.start} ~ {args.end} | 池子 {args.universe}）")
    print("=" * 118)
    print("  收益/回撤单位：%；alpha 为年化超额；无基准时 alpha/beta/IR 为 None")
    print(show.to_string(index=False))

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
        "universe": args.universe,
        "universe_note": uni_note,
        "benchmark": getattr(cfg.backtest, "benchmark", None),
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
    # 文件名带池子口径，避免两套结果互相覆盖
    suffix = f"_{args.tag}" if args.tag else ""
    out_path = OUT_DIR / f"ab_result_{args.universe}{suffix}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[落盘] {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
