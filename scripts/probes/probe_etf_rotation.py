# -*- coding: utf-8 -*-
"""阶段 C：ETF 动量轮动（严格按 `docs/阶段C-ETF轮动预注册.md` 的参数执行）。

⚠️ 本探针的参数来自**预注册文档**，跑之前不能改、跑之后也不能改。
要改先在文档里开一节写「修订原因 + 修订前实测数字」，再改这里。

预注册要点
----------
- 池：境内权益 ETF（剔 债券/货币/商品/跨境）；上市 ≥ 180 交易日；
  近 20 日日均成交额 ≥ 2,000 万元。
- 信号：动量 = **过去 120 交易日累计全收益，跳过最近 5 日**（t−125 ~ t−5）。
- 持有 K = 10，等权；调仓 M = 21 交易日。
- 恰好 3 个配置 C1/C2/C3（N = 120 / 60 / 250），其余参数完全相同。
- 零假设：**同池等权 + 同频再平衡**（主）、同池等权买入持有（辅）。
- 成本：ETF 费率（佣金万 2.5 / 最低 5 元 / 印花税 0 / 过户费 0 / 滑点千 1）。
- 判据 P1：相对主零假设的**年化超额 t ≥ 2**；不通过即判阶段 C 失败。

⛔ 绝对年化不可引用（`etf_list` 是现役快照）→ 只报相对同池的超额。

用法
----
    python scripts/probes/probe_etf_rotation.py
    python scripts/probes/probe_etf_rotation.py --start 2019-01-01 --capital 100000

只读：不写任何数据产物。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve()
ROOT = HERE
while ROOT and not (ROOT / "config" / "base.yaml").exists():
    if ROOT.parent == ROOT:
        break
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from aq.data.etf_store import (  # noqa: E402
    EtfBarStore, EtfNavStore, PROJECT_ROOT, load_etf_list,
)

# ---------------------------------------------------------------- 预注册常量（勿改）
N_GRID = (120, 60, 250)     # C1 / C2 / C3 的动量窗口
SKIP = 5                    # 跳过最近 5 日
K = 10                      # 持有只数
M = 21                      # 调仓周期（交易日）
MIN_AGE = 180               # 上市满 180 交易日
MIN_AMOUNT = 2e7            # 近 20 日日均成交额

EXCLUDE_ASSET: dict[str, tuple[str, ...]] = {
    "债券": ("债", "国债", "政金", "城投", "短融", "信用", "转债", "利率债", "地方债"),
    "货币": ("货币", "现金", "日利", "添益", "添利", "保证金", "理财", "宝利", "快线",
             "快钱", "日日鑫", "天天金", "财富宝", "增益"),
    "商品": ("黄金", "豆粕", "有色", "能源", "原油", "商品", "白银", "铜", "铁矿石"),
    "跨境": ("纳指", "纳斯达克", "标普", "道琼斯", "恒生", "恒指", "港股", "H股",
             "日经", "德国", "法国", "日本", "中概", "海外", "东南亚", "沙特",
             "美国", "越南", "印度", "亚太", "全球", "QDII"),
}

# ETF 费率（与 aq/core/rules.py FEE_OVERRIDES["etf"] 一致，**不自设**）
COMMISSION = 0.00025
MIN_COMMISSION = 5.0
SLIPPAGE = 0.001
STAMP_TAX = 0.0
TRANSFER_FEE = 0.0

TRADING_DAYS = 252


def is_equity(name: str) -> bool:
    return not any(k in name for kws in EXCLUDE_ASSET.values() for k in kws)


def metrics(r: pd.Series, rf_daily: float, per: int = M) -> dict:
    r = r.dropna()
    n = len(r)
    if n < 5:
        return {}
    af = TRADING_DAYS / per
    years = n / af
    growth = float((1.0 + r).prod())
    cagr = growth ** (1.0 / years) - 1.0
    sd = float(r.std(ddof=1))
    vol = sd * np.sqrt(af)
    sharpe = ((float(r.mean()) - rf_daily * per) / sd * np.sqrt(af)) if sd > 0 else float("nan")
    curve = (1.0 + r).cumprod()
    mdd = float((curve / curve.cummax() - 1.0).min())
    return {"n": n, "years": years, "cagr": cagr, "vol": vol,
            "sharpe": sharpe, "max_dd": mdd}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--capital", type=float, default=100000.0,
                    help="回测本金（元）—— 最低 5 元佣金在小额下是硬伤，必须按本金算")
    args = ap.parse_args()

    rf_daily = 0.02 / TRADING_DAYS
    nav = EtfNavStore()
    bars = EtfBarStore()
    lst = load_etf_list()
    name_of = dict(zip(lst["symbol"].astype(str), lst["name"].astype(str)))

    print("=" * 86)
    print("阶段 C：ETF 动量轮动（按预注册文档执行，参数不可改）")
    print("=" * 86)
    print(f"窗口 {args.start} ~ {args.end or '最新'} | K={K} | M={M} | "
          f"跳过最近 {SKIP} 日 | 本金 {args.capital:,.0f} 元")
    print(f"配置：C1 N={N_GRID[0]} / C2 N={N_GRID[1]} / C3 N={N_GRID[2]}")

    # ---- 1) 建全收益面板（官方日增长率）
    daily, meta = {}, {}
    skipped = 0
    for code in nav.available():
        nm = name_of.get(code, name_of.get(code.split(".")[0], ""))
        if not is_equity(nm):
            continue
        try:
            s = nav.total_return(code, start=args.start, end=args.end)
        except Exception:
            skipped += 1
            continue
        s = s.dropna()
        if len(s) < MIN_AGE + N_GRID[2] + SKIP + 10:
            continue
        if s.abs().max() == 0:      # 零波动 = 数据缺陷（货基残留）
            skipped += 1
            continue
        daily[code] = s
        meta[code] = nm
    print(f"\n境内权益候选  : {len(daily)} 只（剔除/缺陷 {skipped} 只）")
    if len(daily) < 50:
        print("!! 候选太少")
        return 1

    R = pd.DataFrame(daily).sort_index()
    G = (1.0 + R.fillna(0.0)).cumprod()      # 增长指数（缺日用 0 收益中性填充）
    cal = R.index
    reb_pos = list(range(0, len(cal) - M, M))
    print(f"交易日历      : {len(cal)} 天 | 调仓期 {len(reb_pos)} 期")

    # ---- 2) 流动性 / 上市年龄（来自日线）
    elig = pd.DataFrame(False, index=[cal[p] for p in reb_pos], columns=R.columns)
    have_bars = 0
    for code in R.columns:
        if not bars.has(code):
            continue
        have_bars += 1
        try:
            b = bars.load(code)
        except Exception:
            continue
        b = b.sort_values("time").set_index("time")
        if "amount" not in b.columns:
            continue
        a20 = b["amount"].rolling(20).mean()
        age = pd.Series(np.arange(len(b)), index=b.index)
        d = pd.DatetimeIndex([cal[p] for p in reb_pos])
        a20r = a20.reindex(d, method="ffill")
        ager = age.reindex(d, method="ffill")
        ok = (a20r >= MIN_AMOUNT) & (ager >= MIN_AGE)
        elig[code] = ok.fillna(False).values
    no_bars = int(R.shape[1] - have_bars)
    print(f"有日线可用    : {have_bars} 只（用于成交额/年龄过滤）")
    print(f"⚠️ 无日线      : {no_bars} 只 → 无法判流动性，**整只剔除**"
          f"（池子因此被抓取覆盖率绑架，不是被规则决定）")
    if no_bars > 0.2 * R.shape[1]:
        print(f"❌ 日线覆盖率仅 {have_bars / R.shape[1]:.1%} < 80% → "
              f"本轮结果**不可用于判据**（池子不完整）")
    print(f"各期可入选只数: 最少 {int(elig.sum(axis=1).min())} / "
          f"中位 {int(elig.sum(axis=1).median())} / 最多 {int(elig.sum(axis=1).max())}")

    # ---- 3) 池内「每期收益面板」（与 N 无关，只算一次）
    #     行 = 调仓期，列 = ETF；单元格 = 该期买入持有到下一调仓日的收益
    pool_rows: dict[pd.Timestamp, pd.Series] = {}
    for p in reb_pos:
        if p + M >= len(cal):
            break
        d = cal[p]
        ok = elig.loc[d]
        ok = ok[ok].index
        if len(ok) < K + 5:
            continue
        per_ret = (G.loc[cal[p + M], ok] / G.loc[cal[p], ok] - 1.0)
        per_ret = per_ret.replace([np.inf, -np.inf], np.nan).dropna()
        if len(per_ret) < K + 5:
            continue
        pool_rows[d] = per_ret
    POOL = pd.DataFrame(pool_rows).T.sort_index()
    print(f"有效调仓期    : {POOL.shape[0]} 期 | 每期池宽 中位 "
          f"{int(POOL.notna().sum(axis=1).median())} 只")

    # ---- 4) 逐配置：选股 + 持有
    def cost_rate_at(turnover: float) -> float:
        """给定单边换手率 τ，算每期成本率（占本金）。

        τ=1 → 全部持仓换掉（成本上界，对策略最不利，判据就按这个算）。
        订单数 ≈ 2·τ·K（买 τK 只 + 卖 τK 只），每单金额 = 本金/K。
        """
        per_order = args.capital / K
        fee_per_order = max(per_order * COMMISSION, MIN_COMMISSION)
        n_orders = 2.0 * turnover * K
        return (n_orders * fee_per_order
                + 2.0 * turnover * args.capital
                * (SLIPPAGE + STAMP_TAX + TRANSFER_FEE)) / args.capital

    def run_config(N: int) -> tuple[pd.Series, pd.Series, float, float]:
        """返回 (策略期收益[已扣保守成本], 零假设期收益, 保守成本率, 实际平均换手)。"""
        strat, bench, picks = [], [], []
        for d in POOL.index:
            p = int(np.where(cal == d)[0][0])
            if p - N - SKIP < 0:
                continue
            ok = POOL.loc[d].dropna().index
            # 动量：G[t-5] / G[t-N-5] - 1  （跳过最近 SKIP 日）
            g_now = G.loc[cal[p - SKIP], ok]
            g_pre = G.loc[cal[p - N - SKIP], ok]
            mom = (g_now / g_pre - 1.0).replace([np.inf, -np.inf], np.nan).dropna()
            if len(mom) < K + 5:
                continue
            pick = mom.sort_values(ascending=False).head(K).index
            strat.append(float(POOL.loc[d, pick].mean()))
            bench.append(float(POOL.loc[d].mean()))
            picks.append(set(pick))
        s = pd.Series(strat)
        b = pd.Series(bench)

        # 实际换手（相邻两期持仓的重合度）
        taus = [1.0]
        for i in range(1, len(picks)):
            taus.append(1.0 - len(picks[i] & picks[i - 1]) / float(K))
        tau_hat = float(np.mean(taus))

        # ⚠️ 判据用 τ=1 的**成本上界**（预注册没约定换手，取对策略最不利的）
        cost_rate = cost_rate_at(1.0)
        s_gross = s.copy()          # 未扣成本 —— 用来区分「信号差」 vs 「成本杀死」
        s = s - cost_rate
        return s, b, cost_rate, tau_hat, s_gross

    print("\n" + "=" * 86)
    print("结果")
    print("=" * 86)

    # ---- 辅助零假设：同池等权「买入持有」（首期建仓，此后权重随价格漂移、不调仓）
    first = POOL.index[0]
    bh_codes = POOL.loc[first].dropna().index
    w = pd.Series(1.0 / len(bh_codes), index=bh_codes)
    bh_rets = []
    for d in POOL.index:
        r = POOL.loc[d].reindex(bh_codes).fillna(0.0)
        bh_rets.append(float((w * r).sum()))
        w = w * (1.0 + r)
        w = w / w.sum()
    bench_bh = pd.Series(bh_rets, index=POOL.index)
    mbh = metrics(bench_bh, rf_daily)
    print(f"\n【辅助零假设】同池等权买入持有（{len(bh_codes)} 只，首期 {first.date()}）")
    print(f"    年化 {mbh['cagr']:>7.2%} | 波动 {mbh['vol']:>7.2%} | "
          f"夏普 {mbh['sharpe']:>5.2f} | 回撤 {mbh['max_dd']:>8.2%}")

    series = {}
    bench_main = None
    for N in N_GRID:
        s, b, cost, tau_hat, s_gross = run_config(N)
        series[N] = s
        bench_main = b
        m = metrics(s, rf_daily)
        mb = metrics(b, rf_daily)

        def _t(strat: pd.Series, base: pd.Series) -> tuple[float, float]:
            """(年化超额, t)。两序列同频同时段。

            ⚠️ t 对线性缩放不变：年化不能改变 t。
            正确口径 = 预注册文档写的「t = 夏普 × √年数」
                     = (X̄/s)·√P × √(n/P) = X̄·√n/s = mean/(sd/√n)
            即**不要再乘/除任何年化因子**（早期版本多除了 √(252/M)，
            把 t 压低 √(252/21)=3.46 倍，已修）。
            """
            ex = (strat.values - base.values)
            ex = pd.Series(ex).dropna()
            n = len(ex)
            sd = float(ex.std(ddof=1))
            if sd <= 0 or n < 3:
                return float("nan"), float("nan")
            t = float(ex.mean()) / (sd / np.sqrt(n))
            return float(ex.mean()) * (TRADING_DAYS / M), t

        ex_ann, t_ex = _t(s, b)
        # 辅助零假设对齐：策略序列比买入持有少开头几期 → 取尾部
        bh_tail = bench_bh.iloc[-len(s):].reset_index(drop=True)
        ex_ann_bh, t_bh = _t(s.reset_index(drop=True), bh_tail)

        print(f"\n【C{N}】动量窗口 N={N}（成本率 {cost:.3%}/期 = 年化 "
              f"{cost * (TRADING_DAYS / M):.2%}，按**换手 τ=1 的上界**算）")
        print(f"    实测平均换手 τ̄ = {tau_hat:.2f} → 若按实际换手，"
              f"成本率 {cost_rate_at(tau_hat):.3%}/期 = 年化 "
              f"{cost_rate_at(tau_hat) * (TRADING_DAYS / M):.2%}"
              f"（**判据不用这个**，只作对照）")
        print(f"    策略  年化 {m['cagr']:>7.2%} | 波动 {m['vol']:>7.2%} | "
              f"夏普 {m['sharpe']:>5.2f} | 回撤 {m['max_dd']:>8.2%}")
        print(f"    零假设年化 {mb['cagr']:>7.2%} | 波动 {mb['vol']:>7.2%} | "
              f"夏普 {mb['sharpe']:>5.2f} | 回撤 {mb['max_dd']:>8.2%}")
        print(f"    超额年化 {ex_ann:>+7.2%} | **t(超额) = {t_ex:+.2f}** | "
              f"夏普提升 {m['sharpe'] - mb['sharpe']:+.2f} | "
              f"{'✅ P1 通过' if abs(t_ex) >= 2 else '❌ P1 不通过（需 |t| ≥ 2）'}")
        print(f"    [辅]对买入持有 超额年化 {ex_ann_bh:>+7.2%} | t = {t_bh:+.2f}")
        # 扣成本前：把「信号差」和「成本杀死」分开
        mg = metrics(s_gross, rf_daily)
        exg_ann, t_gross = _t(s_gross, b)
        print(f"    [未扣成本]年化 {mg['cagr']:>7.2%} | 超额 {exg_ann:>+7.2%} | "
              f"t = {t_gross:+.2f}"
              f"{'  ← 信号本身即为负' if exg_ann < 0 else '  ← 成本才是主因'}")
        print(f"    [P3 回撤]策略 {m['max_dd']:.2%} vs 零假设 {mb['max_dd']:.2%} "
              f"（差 {(m['max_dd'] - mb['max_dd']) * 100:+.2f}pp，"
              f"判据 不劣于 +5pp → "
              f"{'✅' if (m['max_dd'] - mb['max_dd']) >= -0.05 else '❌'}）")

    # ---- 诊断：横截面 IC（只解释结果，不构成新策略、不参与判据）
    print("\n" + "=" * 86)
    print("诊断：动量信号的横截面 IC（Spearman，与下期收益）")
    print("=" * 86)
    print("目的：把「信号没用」和「信号有用但被成本/结构吃掉了」区分开。")
    for N in N_GRID:
        ics = []
        for d in POOL.index:
            p = int(np.where(cal == d)[0][0])
            if p - N - SKIP < 0:
                continue
            ok = POOL.loc[d].dropna().index
            g_now = G.loc[cal[p - SKIP], ok]
            g_pre = G.loc[cal[p - N - SKIP], ok]
            mom = (g_now / g_pre - 1.0).replace([np.inf, -np.inf], np.nan).dropna()
            fwd = POOL.loc[d].reindex(mom.index).dropna()
            common = mom.index.intersection(fwd.index)
            if len(common) < 10:
                continue
            ics.append(mom[common].rank().corr(fwd[common].rank()))
        ic = pd.Series(ics).dropna()
        if len(ic) < 5:
            continue
        t_ic = float(ic.mean()) / (float(ic.std(ddof=1)) / np.sqrt(len(ic)))
        print(f"  N={N:>3}: 平均 IC {ic.mean():+.4f} | IC 标准差 {ic.std(ddof=1):.4f} | "
              f"IC>0 占比 {(ic > 0).mean():.1%} | **t(IC) = {t_ic:+.2f}** | "
              f"{len(ic)} 期")

    # ---- PBO / DSR（跨 3 个配置）
    print("\n" + "=" * 86)
    print("过拟合统计（P4 / P5）")
    print("=" * 86)
    try:
        from aq.backtest.cscv import probability_of_backtest_overfitting, deflated_sharpe
        M_df = pd.DataFrame({f"N{N}": series[N] for N in N_GRID}).dropna()
        if M_df.shape[0] >= 20 and M_df.shape[1] >= 2:
            # ⚠️ 这两个函数返回的是 **dict**，不是 float（直接 f"{x:.3f}" 会 TypeError）
            pres = probability_of_backtest_overfitting(M_df)
            pbo = float(pres["pbo"])
            print(f"PBO（{M_df.shape[1]} 配置 / {M_df.shape[0]} 期）: {pbo:.3f} "
                  f"→ {'✅ ≤0.40' if pbo <= 0.40 else '❌ >0.40'}")
            print(f"    附带：样本外夏普中位数 {pres['median_oos_sharpe']:.3f} | "
                  f"P(OOS<0) {pres['prob_oos_negative']:.3f}")
            best = M_df.mean().idxmax()
            d = deflated_sharpe(M_df[best].values, n_trials=M_df.shape[1],
                                periods_per_year=int(TRADING_DAYS / M))
            dsr = float(d["deflated_sharpe"])
            dsr_t = float(d["t_stat"])
            print(f"DSR（最优配置 {best}，n_trials={M_df.shape[1]}）: {dsr:.3f} "
                  f"| t={dsr_t:+.2f} → "
                  f"{'✅ t ≥ 2' if dsr_t >= 2 else '❌ t < 2'}")
        else:
            print(f"样本不足以算 PBO（{M_df.shape[0]} 期 / {M_df.shape[1]} 配置）")
    except Exception as e:
        print(f"CSCV 计算失败：{type(e).__name__}: {str(e)[:120]}")

    print()
    print("判据口径：P1 = 相对「同池等权 + 同频再平衡」的年化超额 t ≥ 2（不通过即判失败）。")
    print("⛔ 绝对年化不可引用（现役快照，清盘 ETF 不在池里）→ 只看超额。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
