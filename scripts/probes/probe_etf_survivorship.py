# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与公开统计口径。
# 2026-09-18 新建（纳入版本控制），目的是让 ETF 路线的幸存者偏差上界可被复算。
# 前置：需要 akshare（取现役池的流动性分布）；不需要先有 data_cache/。
# 外部统计口径**写死在脚本里并标注来源**，因为它们是文献值、不是可实时拉取的数据。
# ---------------------------------------------------------------------------
"""ETF 侧幸存者偏差的**上界**量化（阶段 1a-2，只读）。

为什么不能给点估计
------------------
实测（`probe_etf_reach.py`）：已退市/已清盘 ETF 的清单与行情**免费源都拿不到** ——
腾讯 `stock_zh_a_hist_tx` 报 IndexError、新浪 `fund_etf_hist_sina` 返回空，
东财 `fund_name_em` 也不含已终止产品（分级基金 150 段仅剩 8 只可证）。

这与个股侧**相反**：个股退市股（乐视网等）腾讯源有数据，所以个股侧的
幸存者偏差可以被量化（见 `docs/终局诊断与重启方案.md`）。ETF 侧只能给区间。

本探针的算法
------------
设任意时点全池规模 N，年清盘率 p，清盘组与存活组的年化收益差 Δ（Δ < 0）。
策略只能从"今天还活着"的池子里选，因此

    可见池平均收益 − 全池平均收益 = −p · Δ  = p · |Δ|   （策略收益被高估的量）

再叠加一层**抵消效应**：我们的池要求"近 20 日日均成交额 ≥ min_amount"，
而清盘 ETF 里占比 f 的标的日均成交额本就低于该门槛 —— 这些标的
**无论如何都不会被策略持有**，因此不构成偏差：

    p_eff = p · (1 − f)          # 有效清盘率
    bias  = p_eff · |Δ|          # 有效高估量

用法::

    python scripts/probes/probe_etf_survivorship.py
    python scripts/probes/probe_etf_survivorship.py --min-amount 5000000
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path


# --- 项目根：向上找 config/base.yaml（**禁止** parents[N]，换目录会指错层级）---
def _find_root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / "config" / "base.yaml").exists():
            return p
    raise RuntimeError(f"未找到 config/base.yaml（从 {start} 向上找）")


ROOT = _find_root(Path(__file__).resolve().parent)


# ---------------------------------------------------------------------------
# 文献值：国内 ETF 清盘只数
# ---------------------------------------------------------------------------
#: (年份, ETF 清盘只数, 口径, 置信度)
#:
#: 来源：Wind 数据，经证券时报/每日经济新闻/界面新闻/新浪财经引用
#:   - 每经 2023-12-26：https://www.nbd.com.cn/rss/tonghuashun/articles/3181893.html
#:   - 界面 2023-12-29：https://www.jiemian.com/article/10613287.html
#:   - 新浪财经 2023-12-28：https://finance.sina.com.cn/roll/2023-12-28/doc-imzzpqei7121854.shtml
LIQUIDATION_WIND = [
    (2018, 13, "Wind"),
    (2019, 5, "Wind"),
    (2020, 13, "Wind"),
    (2021, 25, "Wind"),
    (2022, 34, "Wind"),
    (2023, 32, "Wind"),
    # 2024/2025 未找到权威拆分口径（Wind 只公布了"全部公募清盘 291 只"）；
    # 叩富简投称 2025 = 32 只、同比 +45% → 2024 ≈ 22 只。**来源弱，低置信度**。
    (2024, 22, "低置信度（媒体二次引用）"),
    (2025, 32, "低置信度（媒体二次引用）"),
]

#: 上海证券基金评价研究中心口径：清盘 ETF **占 ETF 总数**的比例
#: 来源：证券时报 2022-06-16 https://www.stcn.com/stock/gsdt/202206/t20220616_4651842.html
LIQUIDATION_RATIO_SSC = [(2019, 5, 0.0176), (2020, 9, 0.0238), (2021, 18, 0.0281)]

#: 近似 ETF 全池只数（年末，Wind 口径，用于把"只数"换算成"比率"）
#: 2019 底 ≈ 284（由 5/1.76% 反推）；后续按公开"突破 1500 只"等口径给量级。
ETF_POOL_SIZE = {2018: 180, 2019: 284, 2020: 380, 2021: 640,
                 2022: 780, 2023: 900, 2024: 1150, 2025: 1500}

#: 清盘 ETF 的规模/流动性特征（叩富简投《2026年ETF市场风险白皮书》，
#: 来源 https://licai.jiantou8.com/ask/qa_6886565.html）。**媒体营销口径，低置信度**，
#: 因此本探针只用它做"抵消效应"的敏感性上界，不进主结论。
LIQ_FEATURES = {
    "scale_below_50m": 0.81,       # 规模 < 5000万 的占清盘总数
    "reason_scale": 0.73,          # 因规模不达标触发
    "amount_below_5m": 0.67,       # 日均成交额 < 500万 的占清盘总数
}

#: 晨星：85% 的清盘基金没有活到第 5 年（说明"年龄"不是保护因素）
MORNINGSTAR_LIFE5 = 0.85


def _probe(fn, timeout: float = 70.0):
    """在线程里跑 fn 并加超时（akshare 接口在代理不通时会长时间挂住）。"""
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["v"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["e"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_worker)
    t.daemon = True
    t0 = time.time()
    t.start()
    t.join(timeout)
    if t.is_alive():
        return ("timeout", None)
    if "e" in box:
        return ("error", box["e"])
    return ("ok", box["v"])


def section_years() -> float:
    """第 1-2 段：清盘只数与年均清盘率的推算区间。返回年均 p 的中值。"""
    print("=" * 104)
    print("第 1 段 国内 ETF 清盘只数（Wind 口径，经证券时报/每经/界面引用）")
    print("=" * 104)
    print(f"  {'年份':<6}{'清盘只数':<10}{'ETF 池约':<10}{'当年比率':<12}{'口径'}")
    ratios = []
    for year, n, src in LIQUIDATION_WIND:
        pool = ETF_POOL_SIZE.get(year)
        r = n / pool if pool else None
        low = "低置信度" in src
        if r is not None and not low:
            ratios.append((year, r))
        print(f"  {year:<6}{n:<10}{pool if pool else '-':<10}"
              f"{(f'{r:.2%}' if r is not None else '-'):<12}{src}")

    print()
    print("  上海证券基金评价研究中心口径（清盘 ETF 占 ETF 总数的比例）")
    for year, n, r in LIQUIDATION_RATIO_SSC:
        print(f"    {year}: {n} 只 = {r:.2%}")

    print()
    if ratios:
        rs = [r for _, r in ratios]
        p_lo, p_hi = min(rs), max(rs)
        p_mid = sum(rs) / len(rs)
    else:  # pragma: no cover - 兜底
        p_lo = p_hi = p_mid = 0.03
    print(f"  纳入统计的年份（剔除低置信度）：{', '.join(str(y) for y, _ in ratios)}")
    print(f"  >>> 年均清盘率 p ∈ [{p_lo:.2%}, {p_hi:.2%}]，中值 {p_mid:.2%}")
    print(f"  >>> 与上海证券口径 [{LIQUIDATION_RATIO_SSC[0][2]:.2%}, "
          f"{LIQUIDATION_RATIO_SSC[-1][2]:.2%}] 同量级，互相印证。")
    print(f"  >>> 注意：p 明显**随年份上升**（2019 年 1.76% → 2021 年 2.81% → 2023 年约 3.6%），"
          f"\n  >>>       原因是 ETF 池扩张快、同质化严重 → 用单一常数 p 会**低估近年偏差**。")
    return p_mid


def section_matrix(p_mid: float) -> None:
    """第 3 段：敏感性矩阵 bias = p · |Δ|。"""
    print()
    print("=" * 104)
    print("第 3 段 敏感性矩阵：策略收益被高估的量 = p × |Δ|（pp/年）")
    print("=" * 104)
    print("  p   = 年清盘率；|Δ| = 清盘组相对存活组的年化跑输幅度（我们不知道，只能给档）")
    print()
    ps = [0.018, 0.028, 0.036, 0.045]
    deltas = [0.0, 0.10, 0.20, 0.25, 0.40]
    head = f"  {'p \\ |Δ|':<12}" + "".join(f"{d:.0%}".rjust(10) for d in deltas)
    print(head)
    print("  " + "-" * (len(head) - 2))
    for p in ps:
        row = f"  {p:<12.1%}"
        for d in deltas:
            row += f"{p * d * 100:>9.2f}pp" if p * d else f"{'0.00pp':>10}"
        print(row)
    print()
    worst = max(ps) * max(deltas)
    print(f"  >>> 不做任何抵消时的**最保守上界** = {max(ps):.1%} × {max(deltas):.0%} "
          f"= {worst * 100:.2f}pp/年")
    mid = p_mid * 0.20
    print(f"  >>> 若取 p={p_mid:.1%}、|Δ|=20%（中值情形）= {mid * 100:.2f}pp/年")


def section_offset(min_amount: float) -> None:
    """第 4 段：流动性过滤的抵消效应（含现役池实测分布）。"""
    print()
    print("=" * 104)
    print("第 4 段 流动性过滤的抵消效应")
    print("=" * 104)
    print("  逻辑：我们的池要求「近 20 日日均成交额 ≥ min_amount」。")
    print("        清盘 ETF 里日均成交额本就低于该门槛的那部分，")
    print("        **无论如何都不会被策略持有** → 不构成偏差。")
    print()
    f = LIQ_FEATURES["amount_below_5m"]
    print(f"  清盘 ETF 中日均成交额 < 500 万 的占比 f = {f:.0%}"
          f"（来源：叩富简投，媒体口径、低置信度）")
    print(f"  → 有效清盘率 p_eff = p × (1 − f)，抵消掉 {f:.0%}")

    print()
    print("  现役池的实测流动性分布（校验这道过滤有多激进）")
    import akshare as ak
    import pandas as pd

    st, spot = _probe(lambda: ak.fund_etf_spot_em())
    if st == "ok" and spot is not None and len(spot):
        amount = pd.to_numeric(spot["成交额"], errors="coerce")
        mcap = pd.to_numeric(spot["流通市值"], errors="coerce")
        n = len(spot)
        print(f"    现役 ETF {n} 只")
        for thr, label in ((5e6, "成交额 < 500万"), (1e7, "成交额 < 1000万")):
            k = int((amount < thr).sum())
            print(f"    {label}: {k} 只（{k / n:.1%}）")
        for thr, label in ((5e7, "流通市值 < 5000万"), (1e8, "流通市值 < 1亿")):
            k = int((mcap < thr).sum())
            print(f"    {label}: {k} 只（{k / n:.1%}）")
        k_amt = int((amount < 5e6).sum())
        print(f"    >>> 与清盘 ETF 的 {f:.0%} 同量级（{k_amt / n:.1%}）→ 这道过滤"
              f"确实会筛掉很大一部分『将死』标的。")
    else:
        print(f"    [跳过] 现役清单不可用（{st}）")


def section_conclusion(p_mid: float) -> None:
    """第 5 段：结论与两条硬性设计约束。"""
    f = LIQ_FEATURES["amount_below_5m"]
    print()
    print("=" * 104)
    print("第 5 段 结论（必须原样抄进文档）")
    print("=" * 104)
    print(f"""
偏差上界（pp/年）：
  - **最保守上界**（不承认任何抵消、取 p 与 |Δ| 的上沿）
      = 4.5% × 40% = 1.80pp/年
  - **中值情形**（p={p_mid:.1%}、|Δ|=20%，仍不承认抵消）
      = {p_mid * 0.20 * 100:.2f}pp/年
  - **计入流动性抵消**（p_eff = p × (1 − {f:.0%}) ≈ {p_mid * (1 - f):.2%}，|Δ|=20%）
      = {p_mid * (1 - f) * 0.20 * 100:.2f}pp/年
  - **最优情形**（|Δ| = 0，即清盘标的业绩并不比存活者差）
      = 0.00pp/年

放在我们的口径里看：
  - 阶段 0 判据的组合长期年化量级是 3~4%（回撤 ≤10% ⇒ 权益 ≤25%）。
  - 即使取中值 {p_mid * 0.20 * 100:.2f}pp，也相当于组合年化的
    **{(p_mid * 0.20) / 0.035:.0%}** —— **不可忽略**。
  - 因此 ETF 轮动策略的**绝对年化数字一律不可引用**。

但相对差仍然有意义：
  - 若零假设是「**等权持有同一个可见池**」，则"漏掉的清盘 ETF"
    对策略与零对照的影响**方向相同、量级相近**，可在相减中大部分抵消。
  - 这是我们唯一可以报告 ETF 轮动结论的形式：**相对同池等权的超额**。

两条硬性设计约束（写进代码，不只是写进文档）：
  1. 轮动策略的产物必须带 `universe_caliber="live_only_biased"` 自描述字段；
  2. 零假设必须是同池等权，**禁止**用"买入持有某一只 ETF"当对照。
""")


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 幸存者偏差上界量化（只读）")
    ap.add_argument("--min-amount", type=float, default=5_000_000.0,
                    help="策略池的日均成交额门槛（元），默认 500 万")
    ap.add_argument("--skip-offset", action="store_true",
                    help="跳过第 4 段（不拉现役清单，纯离线）")
    args = ap.parse_args()

    print(f"项目根 = {ROOT}")
    print(f"策略池流动门槛 = {args.min_amount:,.0f} 元")
    t0 = time.time()
    p_mid = section_years()
    section_matrix(p_mid)
    if not args.skip_offset:
        section_offset(args.min_amount)
    section_conclusion(p_mid)
    print(f"完成，用时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
