# -*- coding: utf-8 -*-
"""CSCV + Deflated Sharpe 稳健性检验（报告 8.3 最高优先级）。

为什么这是当前最该做的事
------------------------
7.12 已经证明：收益对风控流动性门槛**非单调**（1e8 → +11.47%、2e7 →
−19.30%、0 → +6.29%）。一个"参数随便动一动结果就翻号"的策略，
在做过拟合检验之前**不能声称有效** —— 否则所谓"最优参数"只是
历史噪声的最大值，不是可复现的规律。

本脚本做的事
-----------
1. 在一组**我们真的搜过的**配置上各跑一次完整回测，取各自的**日收益序列**
   （不是聚合指标——CSCV 要的是逐日序列）。
2. 拼成 (T × N) 矩阵，跑 CSCV 求 PBO（回测过拟合概率）。
3. 对全样本最优配置跑 DSR，并给出"试过多少次"的敏感性表。
4. 全部落盘，供报告引用。

为什么日收益要缓存
------------------
单次回测 5~10 分钟，N=6 就是近一小时。CSCV 的组合数学部分几乎零成本，
真正贵的是回测本身。所以**日收益按配置缓存**，
之后改 S、改 PBO 参数、追加配置都不用重跑回测。

跑法
----
    # 首次：跑 6 个配置（调仓间隔 × 风控门槛，单变量路径）
    python scripts/cscv_test.py --universe hs300 --tag main

    # 只换统计参数重算（秒级，吃缓存）
    python scripts/cscv_test.py --universe hs300 --tag main --only-stats

    # 追加"权重方案"维度，把 N 扩到 12（增量，已跑过的不重跑）
    python scripts/cscv_test.py --universe hs300 --tag main --weights prior,ic
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

from aq.backtest import cscv as C  # noqa: E402
from aq.core.rules import TRADING_DAYS_PER_YEAR  # noqa: E402

FACTOR_DIR = PROJECT_ROOT / "runtime" / "factor_research"
CSC_DIR = PROJECT_ROOT / "runtime" / "cscv"


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CSCV + Deflated Sharpe 稳健性检验")
    # 口径（2026-09-17 修正）：默认与因子面板对齐 = liquid 池 + 2019 起点。
    # 旧默认是 hs300 池 + 2021-01-01，比面板少用了 35% 样本（1371 vs 1861 天），
    # 且池子口径与所有结论（liquid）不一致。样本量与检验功效直接相关，故对齐。
    # 旧结果保留在 runtime/cscv/{hs300_main,liquid_main}（2021 起点），未改动。
    p.add_argument("--start", default="2019-01-01")
    p.add_argument("--end", default="2026-08-31")
    p.add_argument("--capital", type=float, default=1_000_000.0)
    p.add_argument("--universe", default="liquid", choices=["hs300", "liquid", "all"])
    p.add_argument("--variant", default="full_neu")
    p.add_argument("--tag", default="main", help="结果目录后缀，区分不同实验")
    p.add_argument("--ic-raw", default="",
                   help="未中性化变体目录（如 runtime/factor_research/full_raw）。"
                        "walk-forward 的'中性化抗性门控'需要它做分母，"
                        "不传则该门控恒真")

    # --- 配置网格 ---
    p.add_argument("--hold-list", default="1,5,10,20",
                   help="调仓间隔候选（交易日）")
    p.add_argument("--turnover-list", default="0,2e7,1e8",
                   help="风控单日成交额门槛候选（元）")
    p.add_argument("--weights", default="ic",
                   help="权重方案候选，逗号分隔（ic / prior）")
    p.add_argument("--base-hold", type=int, default=10)
    p.add_argument("--base-turnover", type=float, default=0.0)
    p.add_argument("--wf-window", type=int, default=504,
                   help="walk-forward 定权的回看窗口（交易日）")
    p.add_argument("--wf-refit-every", type=int, default=60,
                   help="walk-forward 重定权间隔（交易日）")
    p.add_argument("--wf-corr-mode", default="ic", choices=["ic", "static"],
                   help="去冗余矩阵口径：ic=滚动（无前视）/ static=全样本（有前视，仅作对照）")
    p.add_argument("--grid", default="both", choices=["both", "hold", "turnover"],
                   help="both = 单变量路径（从基准点出发，每次只动一个轴）")

    # --- 统计参数 ---
    p.add_argument("--n-subperiods", default="8,12,16,20",
                   help="CSCV 子期数；给多个值做稳定性表")
    p.add_argument("--n-trials", type=int, default=0,
                   help="DSR 的试错次数；0 = 用配置数，并另给敏感性表")
    p.add_argument("--trials-grid", default="1,5,10,25,50,100,200,500,1000")
    p.add_argument("--risk-free", type=float, default=0.02,
                   help="年化无风险利率。**必须与 metrics.py 一致（默认 2%）**："
                        "指标里的夏普是 (mu - rf/242)/sd*sqrt(242)，"
                        "若这里不扣，CSCV/DSR 会用原始收益，夏普被高估 3~12 倍")
    p.add_argument("--excess", action="store_true",
                   help="同时对'相对基准的超额收益'再算一遍（剥离 beta）")

    # --- 运行控制 ---
    p.add_argument("--force-panel", action="store_true")
    p.add_argument("--refresh", action="store_true", help="忽略缓存重跑回测")
    p.add_argument("--only-stats", action="store_true",
                   help="只从缓存重算统计，不跑回测")
    return p.parse_args(argv)


def _f(v):
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return float("nan")


def _nums(s: str) -> list[float]:
    return [float(x) for x in str(s).split(",") if x.strip() != ""]


def _apply_universe(cfg, universe: str) -> str:
    """与 ab_weight_test._apply_universe 保持一致的池子口径。"""
    if universe == "hs300":
        cfg.universe.index = "hs300"
        return "沪深300 成分池（含幸存者偏差，研究口径已转 liquid，此为可交易口径）"
    cfg.universe.index = "all"
    cfg.universe.min_list_days = 180
    cfg.universe.min_turnover = 2e7
    cfg.universe.lookback = 20
    cfg.universe.max_symbols = 0
    return "全市场流动性池（研究口径）"


# --------------------------------------------------------------------------
# 配置网格
# --------------------------------------------------------------------------


def build_configs(args) -> list[dict]:
    """构造要跑的配置列表。

    为什么用"单变量路径"而不是全网格
    --------------------------------
    全网格 4×3×2 = 24 个配置 × 7 分钟 ≈ 3 小时，太贵。
    单变量路径从基准点出发每次只动一个轴，6~8 个配置就能覆盖
    "每个轴各搜过一遍"这件事，而这正是 PBO 要检验的对象。
    想扩大 N 时用 --weights 追加维度，已跑过的会命中缓存。
    """
    holds = [int(x) for x in str(args.hold_list).split(",") if x.strip()]
    turnovers = _nums(args.turnover_list)
    weights = [w.strip() for w in str(args.weights).split(",") if w.strip()]
    base_w = weights[0]

    cfgs: list[dict] = []
    seen: set[tuple] = set()

    def add(w, h, t):
        key = (w, h, float(t))
        if key in seen:
            return
        seen.add(key)
        cfgs.append({"weight_source": w, "hold": h, "turnover": float(t),
                     "label": f"w{w}_h{h}d_t{t:g}"})

    if args.grid in ("both", "turnover"):
        for t in turnovers:
            add(base_w, args.base_hold, t)
    if args.grid in ("both", "hold"):
        for h in holds:
            add(base_w, h, args.base_turnover)
    for w in weights[1:]:
        for h in holds:
            add(w, h, args.base_turnover)
    return cfgs


# --------------------------------------------------------------------------
# 回测 + 日收益
# --------------------------------------------------------------------------


def _bench_daily(cfg, dates: list) -> pd.Series | None:
    """取基准的日收益序列，按策略交易日对齐。取不到就返回 None（不猜）。"""
    code = getattr(cfg.backtest, "benchmark", "") or ""
    if not code or not dates:
        return None
    try:
        from aq.data.index_store import IndexBarStore

        store = IndexBarStore()
        idx = store.load(code, start=min(dates), end=max(dates))
    except Exception:  # noqa: BLE001
        return None
    s = pd.Series([float(c) for c in idx["close"]],
                  index=[pd.Timestamp(t).date() for t in idx["time"]])
    s = s[~s.index.duplicated()].sort_index()
    return s.pct_change()


def run_one(cfg, spec: dict, cache_dir: Path, args) -> tuple[pd.Series, dict, pd.Series | None]:
    """跑一个配置，返回 (日收益序列, 指标字典, 基准日收益)。

    有缓存就直接读——CSCV 真正贵的是回测，不是组合数学。
    """
    from aq.backtest.engine import BacktestEngine

    label = spec["label"]
    rpath = cache_dir / f"{label}.csv"
    mpath = cache_dir / f"{label}.meta.json"
    bpath = cache_dir / f"{label}.bench.csv"

    cfg.model.weight_source = spec["weight_source"]
    cfg.risk.liquidity_min_turnover = spec["turnover"]
    # ⚠️ rebalance_days 必须在**构造引擎之前**写进 cfg：
    # 它是在 BacktestEngine.__init__ 里被 _parse_rebalance_days 解析的，
    # run() 不接受这个参数，事后改 cfg 对已经建好的引擎无效。
    cfg.backtest.rebalance_days = int(spec["hold"])

    if rpath.exists() and mpath.exists() and not args.refresh:
        r = pd.read_csv(rpath, parse_dates=["date"])
        s = pd.Series(r["ret"].values, index=[d.date() for d in r["date"]])
        meta = json.loads(mpath.read_text(encoding="utf-8"))
        b = None
        if bpath.exists():
            rb = pd.read_csv(bpath, parse_dates=["date"])
            b = pd.Series(rb["ret"].values, index=[d.date() for d in rb["date"]])
        print(f"    [缓存] {label}  {len(s)} 个交易日")
        return s, meta, b

    if args.only_stats:
        raise FileNotFoundError(
            f"--only-stats 但缓存里没有 {label}；去掉该参数先跑回测")

    t0 = time.time()
    engine = BacktestEngine(cfg)
    res = engine.run()
    el = time.time() - t0

    eq = pd.Series([p.equity for p in res.equity],
                   index=[p.time.date() for p in res.equity])
    s = eq.pct_change().dropna()
    s.name = label

    m = res.metrics
    meta = {
        "label": label,
        "weight_source": spec["weight_source"],
        "hold_days": spec["hold"],
        "risk_turnover": spec["turnover"],
        "total_return": getattr(m, "total_return", None),
        "annual_return": getattr(m, "annual_return", None),
        "sharpe": getattr(m, "sharpe", None),
        "max_drawdown": getattr(m, "max_drawdown", None),
        "alpha": getattr(m, "alpha", None),
        "beta": getattr(m, "beta", None),
        "information_ratio": getattr(m, "information_ratio", None),
        "n_trades": len(res.trades),
        "elapsed_sec": round(el, 1),
    }
    meta.update({k: v for k, v in (getattr(res, "diagnostics", None) or {}).items()
                 if k in ("avg_exposure", "avg_holdings", "days_over_90pct",
                          "rejected_orders") or k.startswith("wf_")})

    # walk-forward 审计：每次重定权的时点、窗口、以及当时的完整权重向量。
    # 这是事后回答"样本外收益差，到底是信号不稳还是执行问题"的唯一依据。
    wf = getattr(engine, "_wf", None)
    if wf is not None and wf.history:
        try:
            wf.summary_frame().to_csv(
                cache_dir / f"{label}.wf.csv", index=False, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"      （WF 审计落盘失败：{e}）")

    b = _bench_daily(cfg, list(eq.index))

    # 增量落盘：跑完一个写一个，中断不丢全部
    pd.DataFrame({"date": list(s.index), "ret": list(s.values)}).to_csv(
        rpath, index=False, encoding="utf-8")
    mpath.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if b is not None:
        bb = b.dropna()
        pd.DataFrame({"date": list(bb.index), "ret": list(bb.values)}).to_csv(
            bpath, index=False, encoding="utf-8")
    print(f"    [完成] {label}  {len(s)} 个交易日  {el:.0f}s  夏普 {_f(meta['sharpe']):.3f}")
    return s, meta, b


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


def _print_matrix(meta_rows: list[dict]) -> None:
    df = pd.DataFrame(meta_rows)
    cols = [c for c in ("label", "weight_source", "hold_days", "risk_turnover",
                        "total_return", "annual_return", "sharpe", "max_drawdown",
                        "alpha", "beta", "information_ratio", "avg_exposure",
                        "n_trades") if c in df.columns]
    show = df[cols].copy()
    for c in ("total_return", "annual_return", "max_drawdown", "alpha", "avg_exposure"):
        if c in show.columns and show[c].dtype.kind == "f":
            show[c] = (show[c] * 100).round(2)
    if "risk_turnover" in show.columns:
        show["risk_turnover"] = (show["risk_turnover"] / 1e8).round(3)
    print("\n" + "=" * 120)
    print("配置矩阵（每列一个配置 = CSCV 的一个候选）")
    print("=" * 120)
    print("  收益/回撤/alpha/仓位 单位：%；risk_turnover 单位：亿元")
    print(show.to_string(index=False))


def _print_pbo(res: dict, S: int) -> None:
    print(f"\n--- CSCV / PBO（S={S} 个子期，{res['n_combinations']} 种划分）---")
    print(f"  PBO（回测过拟合概率）      = {res['pbo']:.3f}   {C.interpret_pbo(res['pbo'])}")
    print(f"  ⚠️ 零假设基准线是 0.5，不是 0 —— 低于 0.5 才说明挑参有信息量")
    print(f"  训练集中选中的夏普 中位数  = {res['median_is_sharpe']:.4f}")
    print(f"  该配置样本外夏普 中位数    = {res['median_oos_sharpe']:.4f}")
    _hc = res.get("haircut")
    _hc_s = (f"{_hc*100:.1f}%" if _hc is not None
             else "n/a（样本内夏普 ≤ 0，无从缩水）")
    print(f"  挑参带来的期望缩水 haircut = {_hc_s}")
    print(f"  样本外为负的概率           = {res['prob_oos_negative']*100:.1f}%")
    print(f"  样本外排第 1 的概率        = {res['prob_top1']*100:.1f}%")
    print(f"  全样本最优配置             = {res['fullsample_best']}"
          f"（全样本夏普 {res['fullsample_best_sharpe']:.4f}）")
    print(f"    它的样本外夏普中位数     = {res['best_full_oos_sharpe_median']:.4f}"
          f"，为负占比 {res['best_full_oos_negative_share']*100:.1f}%"
          f"，5% 分位 {res['best_full_oos_p05']:.4f}")


def _print_dsr(best_label: str, rets: pd.Series, cfg_n: int, args) -> dict:
    n_trials = args.n_trials if args.n_trials > 0 else cfg_n
    ppy = TRADING_DAYS_PER_YEAR
    base = C.deflated_sharpe(rets, n_trials=n_trials, periods_per_year=ppy)
    print("\n" + "=" * 120)
    print(f"Deflated Sharpe Ratio —— 以全样本最优配置 {best_label} 为对象")
    print("=" * 120)
    print(f"  观测数 T = {base['n_obs']} 个交易日")
    print(f"  每期夏普 = {base['sharpe']:.6f}  →  年化 {base['sharpe_annual']:.3f}")
    # ⚠️ 必须区分算术年化与几何年化：这里报的是 mean×242，
    # 真实 CAGR 还要再扣掉约 σ²/2 的波动拖累。本例算术 +0.96%、
    # σ=17.2% → CAGR ≈ −0.5%，**跑输 2% 的无风险利率**。
    # 只看算术年化会得出"至少是正的"这种错误印象。
    _cagr = base["annual_return_est"] - 0.5 * base["annual_vol_est"] ** 2
    print(f"  年化收益（算术 mean×{ppy}）≈ {base['annual_return_est']*100:.2f}%"
          f"   年化波动 ≈ {base['annual_vol_est']*100:.2f}%")
    print(f"  几何近似 CAGR ≈ 算术 − σ²/2 = {_cagr*100:.2f}%"
          f"（这才是复利实际拿到的）")
    print(f"  偏度 {base['skew']:.3f}  超额峰度 {base['excess_kurtosis']:.3f}")
    print(f"  夏普估计量标准差 σ_SR = {base['sr_std']:.6f}")
    print(f"  t 统计量（H0: 真实夏普 = 0）= {base['t_stat']:.3f}")
    print(f"  ⚠️ t < 2 意味着**连'显著不为 0'都谈不上**，"
          f"此时多重检验校正只是雪上加霜")

    grid = [int(x) for x in str(args.trials_grid).split(",") if x.strip()]
    rows = C.dsr_sensitivity(rets, trials_grid=grid, periods_per_year=ppy)
    df = pd.DataFrame(rows)[["n_trials", "sharpe_annual", "sr_threshold_annual",
                             "deflated_sharpe", "t_stat"]]
    print("\n  试错次数敏感性（n_trials 是主观输入，故整表摊开）")
    print("  " + "-" * 76)
    print(f"  {'试过 N 次':>10s} {'年化夏普':>10s} {'门槛 SR0(年化)':>16s} "
          f"{'DSR':>9s} {'t 统计':>9s}")
    for r in rows:
        flag = ""
        if r["n_trials"] == n_trials:
            flag = "  ← 本次采用"
        print(f"  {r['n_trials']:>10d} {r['sharpe_annual']:>10.3f} "
              f"{r['sr_threshold_annual']:>16.3f} {r['deflated_sharpe']:>9.3f} "
              f"{r['t_stat']:>9.3f}{flag}")
    print("  " + "-" * 76)
    if args.n_trials <= 0:
        # 默认 N = 配置个数，这是自由度的**下界**，容易被误读成"只试过这么几次"。
        print(f"  ⚠️ 本次采用 N={n_trials}（= 本轮配置数）—— 这是自由度的**下界**，不是真实值。")
        print(f"     它只统计了这一轮跑的 {n_trials} 个配置，不含此前已经扫过的维度：")
        print(f"     hold_days / top_k / score_threshold / min_turnover / industry_max /")
        print(f"     icir-ratio / shrink / cap ... 这些都在同一份样本上选过。")
        print(f"     → 按真实研究自由度（100 量级）读上表对应行的 DSR 才诚实。")
    print("  读法：DSR > 0.95 才算'扣掉试错成本后仍然显著'；"
          "DSR 随 N 迅速塌陷 = 结论完全依赖于'没试过太多次'这个假设")
    return base


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    vdir = FACTOR_DIR / args.variant
    if not (vdir / "summary.csv").exists():
        print(f"[错误] 缺 runtime/factor_research/{args.variant}/summary.csv")
        return 1

    cache_dir = CSC_DIR / f"{args.universe}_{args.tag}" / "rets"
    cache_dir.mkdir(parents=True, exist_ok=True)
    CSC_DIR.mkdir(parents=True, exist_ok=True)

    from aq.config.settings import load_settings

    cfg = load_settings()
    cfg.backtest.start = args.start
    cfg.backtest.end = args.end
    cfg.backtest.initial_cash = args.capital
    cfg.backtest.panel_force = bool(args.force_panel)
    cfg.model.weight_source = "ic"
    cfg.model.ic_summary_path = str(vdir / "summary.csv")
    cfg.model.ic_weight_mode = "icir"
    cfg.model.ic_select = True
    if args.ic_raw:
        cfg.model.ic_raw_summary_path = str(Path(args.ic_raw) / "summary.csv")
    cfg.model.wf_window = int(args.wf_window)
    cfg.model.wf_refit_every = int(args.wf_refit_every)
    cfg.model.wf_corr_mode = str(args.wf_corr_mode)

    uni_note = _apply_universe(cfg, args.universe)
    print(f"[池子] {args.universe} —— {uni_note}")
    print(f"[缓存] {cache_dir}")

    specs = build_configs(args)
    print(f"[网格] {len(specs)} 个配置：" + ", ".join(s["label"] for s in specs))

    # ---- 1) 跑回测 / 读缓存 ----
    series: dict[str, pd.Series] = {}
    bench_series: dict[str, pd.Series] = {}
    meta_rows: list[dict] = []
    for i, spec in enumerate(specs, 1):
        print(f"\n[{i}/{len(specs)}] {spec['label']}  "
              f"(权重 {spec['weight_source']} / 间隔 {spec['hold']}日 / "
              f"成交额门槛 {spec['turnover']:g})")
        s, meta, b = run_one(cfg, spec, cache_dir, args)
        series[spec["label"]] = s
        if b is not None:
            bench_series[spec["label"]] = b
        meta_rows.append(meta)
        # 面板只在第一次强制重建：--force-panel 是"底层数据变了"的信号，
        # 不是"每次回测都要重算"
        if getattr(cfg.backtest, "panel_force", False):
            cfg.backtest.panel_force = False

    _print_matrix(meta_rows)

    # ---- 2) 拼矩阵 ----
    R = pd.DataFrame(series).dropna(how="any")
    if R.shape[1] < 2 or R.shape[0] < 100:
        print(f"\n[错误] 可用矩阵只有 {R.shape}，无法做 CSCV")
        return 1
    print(f"\n[矩阵] {R.shape[0]} 个交易日 × {R.shape[1]} 个配置"
          f"（{R.index[0]} ~ {R.index[-1]}）")

    # ⚠️ 扣无风险利率 —— 这一行直接改变结论，不是可选项。
    # 指标里的 sharpe = (mu - rf/242)/sd*sqrt(242)（metrics.py）。
    # 若拿原始收益做 CSCV/DSR，夏普会被高估 3~12 倍：本实验里
    # h10_t1e8 的原始年化夏普 0.298 看着最好，扣掉 rf 后只剩 0.025，
    # 而 h20d_t0 从 0.171 降到 0.055 反而成了第一 —— 全样本最优配置整个换人。
    # 根因：低仓位低波动的配置（26% 仓位）在"原始夏普"下占尽便宜，
    # 但它的年化收益 1.93% 扣掉 2% 无风险利率后几乎归零。
    rf_daily = args.risk_free / TRADING_DAYS_PER_YEAR
    Rn = R - rf_daily
    print(f"[口径] 已扣无风险利率 {args.risk_free*100:.2f}%/年"
          f"（日 {rf_daily:.3e}），年化用 {TRADING_DAYS_PER_YEAR} 交易日")
    print("       ⚠️ 不扣的话夏普是'收益/波动'，扣了才是'超额收益/波动'；"
          "后者才对应能不能跑赢现金")
    _cmp = []
    for c in R.columns:
        _cmp.append((c, C.annualize(C.sharpe_per_period(R[c]), TRADING_DAYS_PER_YEAR),
                     C.annualize(C.sharpe_per_period(Rn[c]), TRADING_DAYS_PER_YEAR)))
    print(f"       {'配置':<18s}{'原始年化夏普':>14s}{'扣 rf 后':>12s}")
    for c, a, b in _cmp:
        print(f"       {c:<18s}{a:>14.4f}{b:>12.4f}")
    R = Rn

    # ---- 2b) 配置相关性

    # ---- 2b) 配置相关性：决定 PBO 怎么读、以及 DSR 该用多大的 N ----
    corr = C.effective_trials(R)
    print(f"\n[相关性] 配置间平均两两相关 ρ̄ = {corr['mean_pairwise_corr']:.3f}"
          f"（区间 {corr['min_pairwise_corr']:.3f} ~ {corr['max_pairwise_corr']:.3f}）")
    print(f"         有效独立试验数 N_eff = {corr['n_eff']:.2f}"
          f"（{corr['n_configs']} 个配置，但高度相关时远小于此）")
    print(f"         ⚠️ ρ̄ 高时 PBO 天然偏低（候选越像，样本外排名越稳定），"
          f"此时低 PBO 只说明'挑得可复现'，不代表'挑出来的能赚钱'")

    # ---- 3) CSCV / PBO：多个 S 做稳定性表 ----
    Ss = [int(x) for x in str(args.n_subperiods).split(",") if x.strip()]
    pbo_runs = {}
    print("\n" + "=" * 120)
    print("CSCV / PBO —— 回测过拟合概率")
    print("=" * 120)
    for S in Ss:
        try:
            r = C.probability_of_backtest_overfitting(R, n_subperiods=S)
        except Exception as e:  # noqa: BLE001
            print(f"  S={S} 失败：{e}")
            continue
        if "error" in r:
            print(f"  S={S}：{r['error']}")
            continue
        pbo_runs[S] = r
        _print_pbo(r, S)

    if not pbo_runs:
        print("[错误] 所有 S 都算不出来")
        return 1

    # 主口径：优先用 S=16（约 4 个月一段、12870 种划分），没有就用最大的
    main_S = 16 if 16 in pbo_runs else max(pbo_runs)
    main_pbo = pbo_runs[main_S]
    vals = [r["pbo"] for r in pbo_runs.values()]
    print(f"\n  跨 S 稳定性：PBO = " +
          ", ".join(f"S={s}:{r['pbo']:.3f}" for s, r in pbo_runs.items()))
    print(f"  极差 {max(vals)-min(vals):.3f}。"
          f"⚠️ 单次 PBO 估计在零假设下的离散度极大（模拟区间 0.09~0.94），"
          f"所以只有跨 S 一致时结论才可信")

    # ---- 4) DSR ----
    best_label = main_pbo["fullsample_best"]
    dsr_main = _print_dsr(best_label, R[best_label], len(R.columns), args)

    # ---- 5) 超额收益口径（剥离 beta）----
    dsr_ex = None
    pbo_ex = None
    if args.excess and bench_series:
        B = pd.DataFrame(bench_series).dropna(how="any")
        common = R.index.intersection(B.index)
        if len(common) > 100:
            E = R.loc[common] - B.loc[common]
            print("\n" + "=" * 120)
            print("超额口径（策略日收益 − 基准日收益，剥离市场 beta）")
            print("=" * 120)
            r = C.probability_of_backtest_overfitting(E, n_subperiods=main_S)
            if "error" not in r:
                pbo_ex = r
                _print_pbo(r, main_S)
                bl = r["fullsample_best"]
                dsr_ex = C.deflated_sharpe(E[bl], n_trials=len(E.columns))
                print(f"\n  超额口径 DSR（最优 {bl}，N={len(E.columns)}）= "
                      f"{dsr_ex['deflated_sharpe']:.3f}"
                      f"  年化夏普 {dsr_ex['sharpe_annual']:.3f}"
                      f"  t={dsr_ex['t_stat']:.3f}")
        else:
            print("\n[提示] 基准序列对齐不足，跳过超额口径")

    # ---- 6) 结论 ----
    print("\n" + "=" * 120)
    print("结论")
    print("=" * 120)
    t = dsr_main["t_stat"]
    dsr_n = dsr_main["deflated_sharpe"]
    hc = main_pbo.get("haircut")
    hc_s = (f"{hc*100:.1f}%" if hc is not None
            else "n/a（样本内夏普 ≤ 0，本来就没什么可缩水的）")
    # ⚠️ PBO 的零假设基准**不是固定的 0.5**：它是候选集的函数。
    # 候选越相似（ρ̄ 高、N_eff 低），基准越往上移；子期数 S 也会改变它。
    # 用固定 0.5 做二元判断（"大于 0.5 = 过拟合"）已被 7.14.8 的标定实验推翻 ——
    # 5 候选同质时实测基准 0.593、90% 区间宽 0.78，实测值落在区间内即"无法拒绝零假设"。
    _rho = corr["mean_pairwise_corr"]
    _neff = corr["n_eff"]
    lines = [
        f"1. PBO = {main_pbo['pbo']:.3f}（S={main_pbo['n_subperiods']}）"
        f"｜参考基准 0.5，但⚠️ 真实基准随候选集上移："
        f"本次 ρ̄={_rho:.2f}、N_eff={_neff:.2f} → {C.interpret_pbo(main_pbo['pbo'])}",
        f"2. 挑参带来的期望缩水 haircut = {hc_s}",
        f"3. 最优配置 {best_label} 的 t 统计量 = {t:.3f}"
        f"{'  → 连显著不为 0 都谈不上' if abs(t) < 2 else '  → 显著'}",
        f"4. DSR（N={dsr_main['n_trials']}）= {dsr_n:.3f}"
        f"{'  → 不显著' if dsr_n < 0.95 else '  → 显著'}",
    ]
    for ln in lines:
        print("  " + ln)
    if abs(t) < 2:
        print("\n  ⚠️ 结论：现有样本量下，最优配置的超额收益与 0 无法区分。")
        print("     这不是'策略不行'，而是'证据不足以支持任何结论'——")
        print("     继续调参只会把噪声拟合得更好。下一步应当是换思路")
        print("     （换信号源 / 换周期 / 提高信噪比），而不是继续扫参数。")
    elif main_pbo["pbo"] >= 0.5:
        print("\n  ⚠️ 结论：即使 t 显著，挑参动作本身没有样本外预测力，")
        print("     选出的'最优参数'大概率是历史噪声的最大值。")

    # ---- 7) 落盘 ----
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": args.start, "end": args.end},
        "capital": args.capital,
        "universe": args.universe,
        "universe_note": uni_note,
        "benchmark": getattr(cfg.backtest, "benchmark", None),
        "weight_basis": str(vdir.relative_to(PROJECT_ROOT)),
        "configs": meta_rows,
        "matrix": {"n_obs": int(R.shape[0]), "n_configs": int(R.shape[1]),
                   "start": str(R.index[0]), "end": str(R.index[-1])},
        "config_correlation": corr,
        "pbo_by_subperiods": {str(k): v for k, v in pbo_runs.items()},
        "pbo_main": main_pbo,
        "dsr_main": dsr_main,
        "dsr_sensitivity": C.dsr_sensitivity(
            R[best_label],
            [int(x) for x in str(args.trials_grid).split(",") if x.strip()],
            periods_per_year=TRADING_DAYS_PER_YEAR),
        "risk_free_rate": args.risk_free,
        "trading_days_per_year": TRADING_DAYS_PER_YEAR,
        "pbo_excess": pbo_ex,
        "dsr_excess": dsr_ex,
        "verdict": {
            "pbo": main_pbo["pbo"],
            "t_stat": t,
            "dsr": dsr_n,
            "significant": bool(abs(t) >= 2 and dsr_n >= 0.95),
            "note": lines,
        },
    }
    out_path = CSC_DIR / f"cscv_{args.universe}_{args.tag}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2,
                                   default=str), encoding="utf-8")
    R.to_csv(CSC_DIR / f"cscv_{args.universe}_{args.tag}_returns.csv",
             encoding="utf-8")
    print(f"\n[落盘] {out_path}")
    print(f"[落盘] {CSC_DIR / f'cscv_{args.universe}_{args.tag}_returns.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
