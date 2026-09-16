# -*- coding: utf-8 -*-
"""问题归因实验：把"选股环节"与"组合构建环节"的问题分开。

背景
----
A/B 权重对照发现两个方案年化都在 -5% 左右，而同股票池**等权持有**是 +12%。
差距（约 -17pp/年）不可能来自权重分配方式（那只是把同一批信号重新加权），
必须定位到更上游。本脚本用四组对照把问题隔离出来：

    C  等权持有全池        —— 基准，不选股、不做因子
    D  随机选 top_k        —— 只保留"每天换仓 + 持有 top_k"这一结构，
                              把因子完全随机化。若 D ≈ 基准，说明结构没问题；
                              若 D 远差于基准，说明"每日换仓 top_k"本身在毁灭价值
    A  先验权重 top_k      —— 完整策略（先验权重）
    B  IC 权重 top_k       —— 完整策略（IC 权重）

这样就能回答一个关键问题：**负收益是因子选错了，还是"每日换仓 + 集中持仓"
这个结构本身有问题？** 这是 P2 之后所有优化工作的前提。

用真因子值排序（不经过 scorer 的 z-score/tanh 变换），以隔离变换带来的影响。
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

from aq.factors.vlib import SPEC_BY_NAME, spec_names  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--panel", default="")
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--cost-bps", type=float, default=0.0,
                   help="单边成本（基点），如 10 = 0.10%%")
    p.add_argument("--hold-scan", action="store_true",
                   help="额外做调仓频率扫描（1/5/10/20 日），复现报告 7.3 第二张表")
    p.add_argument("--hold-days", default="1,5,10,20",
                   help="扫描的持有期，逗号分隔")
    return p.parse_args(argv)


def _find_panel() -> Path:
    """选面板文件（统一走 scripts._panel_utils，按覆盖度而非 mtime 选）。

    历史教训：这里原本是 ``sorted(..., key=mtime)[-1]``，取到了
    ``panel_all_2021-01-04_...``（**261 只**股票、1372 天）—— 它只是
    最近被生成，并不是覆盖度最高的。而它的因子表现与全市场面板
    **方向相反**（该池上打分五档年化 Q1>Q5，全市场面板则严格单调递增），
    导致整节组合层结论建立在一个有偏小池上。详见报告第 7.5 节。
    """
    from scripts._panel_utils import find_panel

    return find_panel(None)


def _stat(daily: pd.Series, label: str, n_trade: float = np.nan) -> dict:
    cum = float((1 + daily).prod() - 1)
    n_yr = len(daily) / 242
    ann = ((1 + cum) ** (1 / n_yr) - 1) if n_yr > 0 else float("nan")
    # ---------------------------------------------------------------
    # 最大回撤必须基于**复利净值曲线**，不能基于日收益的累加和。
    # 旧实现 `(daily.cumsum() - daily.cumsum().cummax()).min()` 算的是
    # "累计收益率序列相对其历史高点"的落差 —— 它随天数线性发散，可以
    # 轻易低于 -100%（实测随机对照组跑出 -190%），这是**物理上不可能的**
    # 回撤值，会让人误以为脚本算错了所有指标。
    # 正确口径：nav = cumprod(1+r)，回撤 = min(nav / nav.cummax() - 1)，恒在 [-1, 0]。
    # ---------------------------------------------------------------
    nav = (1 + daily).cumprod()
    dd = nav / nav.cummax() - 1.0
    shp = (daily.mean() / daily.std()) * (242 ** 0.5) if daily.std() else float("nan")
    return {
        "label": label,
        "cum_%": round(cum * 100, 2),
        "ann_%": round(ann * 100, 2),
        "sharpe": round(float(shp), 3),
        "maxDD_%": round(float(dd.min()) * 100, 2),
        "win_day_%": round(float((daily > 0).mean()) * 100, 1),
        "avg_turnover": (round(n_trade, 3) if np.isfinite(n_trade) else None),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    panel_path = Path(args.panel) if args.panel else _find_panel()
    print(f"[面板] {panel_path.name}  top_k={args.top_k}  成本={args.cost_bps}bps/边")

    df = pd.read_parquet(panel_path)
    factors = [f for f in spec_names() if f in df.columns]
    need = ["date", "symbol", "fwd_ret_1", "t1_tradable", "close_real"] + factors
    df = df[[c for c in dict.fromkeys(need) if c in df.columns]]
    df = df[df["t1_tradable"].fillna(False)].copy()
    print(f"[样本] {len(df):,} 行 / {df['date'].nunique()} 天 / {df['symbol'].nunique()} 只")

    # ---- 综合分：各因子横截面 z -> 按 library.direction 定向 -> 按权重加权 ----
    lib_dir = {n: SPEC_BY_NAME[n].direction for n in factors}

    def _zscore(f: str) -> pd.Series:
        g = df.groupby("date")[f]
        mu = g.transform("mean")
        sd = g.transform(lambda s: s.std() or 1.0)
        return (df[f] - mu) / sd * lib_dir[f]

    from aq.config.settings import load_settings
    from aq.factors.scoring import build_scorer

    cfg = load_settings()
    cfg.model.weight_source = "prior"
    prior_w = dict(build_scorer(cfg).weights)
    cfg.model.weight_source = "ic"
    cfg.model.ic_summary_path = str(
        PROJECT_ROOT / "runtime" / "factor_research" / "full_neu" / "summary.csv")
    cfg.model.ic_strict = False
    ic_w = dict(build_scorer(cfg).weights)

    def _score(weights: dict) -> pd.Series:
        parts = []
        for f in factors:
            w = abs(weights.get(f, 0.0))
            if w <= 1e-12:
                continue
            parts.append(_zscore(f).fillna(0) * w)
        s = sum(parts)
        return s

    df["_s_prior"] = _score(prior_w)
    df["_s_ic"] = _score(ic_w)
    df["_s_eq"] = sum(_zscore(f).fillna(0) for f in factors) / len(factors)

    rng = np.random.default_rng(args.seed)
    df["_s_rand"] = rng.random(len(df))

    cost = args.cost_bps / 10000.0

    def _run_fast(score_col: str, top_k: int) -> tuple[pd.Series, float]:
        """向量化：每日取 score 最大的 top_k，等权，返回 (日收益, 平均单边换手)。"""
        rk = df.groupby("date")[score_col].rank(method="first", ascending=False)
        hold = df[rk <= top_k]
        daily = hold.groupby("date")["fwd_ret_1"].mean()

        # 换手：相邻两日持仓中"新买入"的只数 / k，即**单边**换手率。
        # 注意口径：成本 = 2 × cost × turnover（因为卖出旧仓 + 买入新仓各付一次），
        # 所以这里的 turnover 必须定义为单边（新买入占比），不能定义成双边。
        sets = hold.groupby("date")["symbol"].apply(set)
        turns = []
        prev = None
        for s in sets:
            if prev is not None:
                turns.append(len(s - prev) / top_k)
            prev = s
        tv = float(np.mean(turns)) if turns else float("nan")
        if cost > 0:
            daily = daily - pd.Series(
                {d: 2 * cost * t for d, t in zip(sets.index[1:], turns)},
            ).reindex(daily.index).fillna(0.0)
        return daily, tv

    rows = []
    base = df.groupby("date")["fwd_ret_1"].mean()
    rows.append(_stat(base, "C 等权持有全池(基准)"))
    for col, label in (
        ("_s_rand", f"D 随机选 top{args.top_k}"),
        ("_s_prior", f"A 先验权重 top{args.top_k}"),
        ("_s_ic", f"B IC权重 top{args.top_k}"),
        ("_s_eq", f"E 全因子等权 top{args.top_k}"),
    ):
        s, tv = _run_fast(col, args.top_k)
        rows.append(_stat(s, label, tv))

    out = pd.DataFrame(rows)
    print("\n" + "=" * 110)
    print("问题归因（% 单位，除 sharpe 与 turnover）")
    print("=" * 110)
    print(out.to_string(index=False))

    print("\n--- 关键对比 ---")
    c = rows[0]
    for r in rows[1:]:
        print(f"  {r['label']:26s} 年化 {r['ann_%']:+7.2f}%  "
              f"vs 基准 {r['ann_%'] - c['ann_%']:+7.2f}pp   换手 {r['avg_turnover']}")
    d_row = rows[1]
    if d_row["ann_%"] < c["ann_%"] - 3:
        print("\n  >>> 随机选股也大幅跑输基准 -> 问题在【每日换仓 top_k 结构】或成本，"
              "不是因子选错")
    else:
        print("\n  >>> 随机选股接近基准 -> 结构没问题，问题在因子/权重")

    if args.hold_scan:
        _run_hold_scan(df, _run_fast, args, base)

    return 0


def _run_hold_scan(df: pd.DataFrame, run_fast, args, base: pd.Series) -> None:
    """调仓频率扫描：每 h 个交易日换一次仓，期间持有不动。

    复现报告 7.3 的第二张表。实现要点：

    - **必须逐日累乘收益**，不能直接用 ``fwd_ret_h`` 的日均值。原因是：
      ① 逐日序列才能算出可与其余组对比的日频夏普；
      ② ``fwd_ret_h`` 在采样点上等价于持有 h 日，但会丢掉持仓期的
         中间波动，回撤也会被严重低估。
    - 持仓日的选择：把交易日序列按 ``[::h]`` 取索引，相邻两个采样点之间
      持有同一组股票。换手只在采样日发生，作为**单边**换手率记录。
    - 成本按 ``(换手 / h)`` 摊到每个持有日 —— 因为一次换仓的成本要由
      h 个持有日共同承担，摊薄后才是"每天的成本率"。
    """
    dates = np.sort(df["date"].unique())
    hold_list = [int(x) for x in str(args.hold_days).split(",") if x.strip()]
    cost = args.cost_bps / 10000.0

    # 因子等权打分（与主表 E 组同口径：z-score 后**乘库方向**再等权）
    #
    # ⚠️ 必须乘 `lib.direction()`。曾漏掉这一步，导致扫描出的 h=1 换手 0.323，
    # 而同口径的主表 E 组是 0.410 —— 换手对不上就是"打分变了"的报警信号。
    # 漏乘方向会让每个因子的正负号被抹掉，等价于给组合一个随机方向。
    from aq.factors.library import FactorLibrary
    lib = FactorLibrary()
    factors = [f for f in spec_names() if f in df.columns]
    z = []
    for f in factors:
        g = df.groupby("date")[f]
        z.append(((df[f] - g.transform("mean"))
                  / g.transform(lambda s: s.std() or 1.0)).fillna(0.0)
                 * lib.direction(f))
    df["_s_eq_scan"] = sum(z) / max(len(z), 1)

    # 自检：h=1 时必须与主表 E 组（_s_eq）的持仓完全一致，否则说明两处打分口径又跑偏了
    if "_s_eq" in df.columns:
        rk_a = df.groupby("date")["_s_eq"].rank(method="first", ascending=False) <= args.top_k
        rk_b = df.groupby("date")["_s_eq_scan"].rank(method="first", ascending=False) <= args.top_k
        # 逐日把两边的 top_k 集合取出来比对（用 (date,symbol) 对，
        # 不能用整表布尔相等去数，那样分母会是全体行数）
        sa = df.loc[rk_a, ["date", "symbol"]].apply(tuple, axis=1)
        sb = df.loc[rk_b, ["date", "symbol"]].apply(tuple, axis=1)
        overlap = len(set(sa) & set(sb))
        if overlap != len(sa):
            print(f"[警告] 扫描打分与主表 E 组口径不一致："
                  f"top{args.top_k} 逐日持仓重叠 {overlap}/{len(sa)}，"
                  f"请检查 z-score/方向定义")

    print("\n" + "=" * 110)
    print(f"调仓频率扫描（等权因子 top{args.top_k}，单边成本 {args.cost_bps}bps，"
          f"成本按持有期摊薄）")
    print("=" * 110)
    print(f"{'持有期':>6s} {'换手':>7s} {'毛年化':>9s} {'成本拖累':>9s} "
          f"{'净年化':>9s} {'夏普':>7s} {'最大回撤':>9s}")

    # ---- 预分组：按日期切好索引，避免在采样循环里对全表反复布尔筛选（O(n²)） ----
    day_idx = {d: g.index for d, g in df.groupby("date", sort=False)}
    sym = df["symbol"].to_numpy()
    row_date = df["date"].to_numpy()

    for h in hold_list:
        # ---- 1) 在采样日选股，得到"日期 -> 持仓集合"的映射 ----
        pick_idx = list(range(0, len(dates), h))
        picks = [dates[i] for i in pick_idx]
        held_on: dict = {}          # 采样日 -> 该期持仓集合

        # 每只股票在每个持有窗口内贡献的行：用 dict 累积 (date -> set(symbol))
        # 这里直接构造"哪些行属于当前持仓"，一次向量化完成
        keep_mask = np.zeros(len(df), dtype=bool)
        for i, d0 in enumerate(picks):
            idx = day_idx.get(d0)
            if idx is None or len(idx) == 0:
                continue
            seg = df.loc[idx, "_s_eq_scan"]
            # 取该日打分最高的 top_k。
            # ⚠️ 用 nlargest 而不是 argpartition：argpartition 会把 NaN 当成
            # 极端值排到首位（-NaN 参与比较的结果未定义），从而选出"没有因子值"
            # 的股票；nlargest 默认丢弃 NaN，与原先 rank() 口径一致。
            sel = seg.nlargest(min(args.top_k, seg.notna().sum()))
            held = set(df.loc[sel.index, "symbol"])
            held_on[d0] = held

            d_next = picks[i + 1] if i + 1 < len(picks) else None
            if d_next is None:
                window = (row_date >= d0)
            else:
                window = (row_date >= d0) & (row_date < d_next)
            # 该窗口内、且股票在 held 中的行
            keep_mask |= window & np.fromiter(
                (s in held for s in sym), dtype=bool, count=len(sym))

        hold = df[keep_mask]
        gross = (hold.groupby("date")["fwd_ret_1"].mean()
                 .reindex(dates).fillna(0.0).sort_index())

        # ---- 2) 换手：相邻两个采样期的持仓集合差异（单边） ----
        turns = []
        prev = None
        for d0 in picks:
            cur = held_on.get(d0, set())
            if prev is not None:
                turns.append(len(cur - prev) / max(args.top_k, 1))
            prev = cur
        tv = float(np.mean(turns)) if turns else float("nan")

        # ---- 3) 成本：一次换仓的双边成本，摊到 h 个持有日 ----
        cost_daily = (2 * cost * tv / h) if cost > 0 else 0.0
        net = gross - cost_daily

        a_g, a_n = _stat(gross, "g"), _stat(net, "n")
        drag = a_g["ann_%"] - a_n["ann_%"]
        print(f"{h:>6d} {tv:7.3f} {a_g['ann_%']:>8.2f}% {drag:>8.2f}pp "
              f"{a_n['ann_%']:>8.2f}% {a_n['sharpe']:>7.3f} "
              f"{a_n['maxDD_%']:>8.2f}%")

    b = _stat(base, "b")
    print(f"\n  参考：基准（同池等权，每日再平衡、零成本）年化 {b['ann_%']:+.2f}%，"
          f"夏普 {b['sharpe']:.3f}，最大回撤 {b['maxDD_%']:.2f}%")


if __name__ == "__main__":
    raise SystemExit(main())
