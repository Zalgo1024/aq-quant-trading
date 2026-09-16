"""因子有效性检验（P2）—— 自研版 alphalens。

为什么不用 alphalens
--------------------
1. alphalens-reloaded 依赖老版本 pandas API，与本项目 pyarrow/pandas 2.x 环境冲突；
2. 它按"美股日历 + 无涨跌停"假设设计，A 股的一字板不可交易问题它不处理；
3. 我们要的 IC 衰减 / 中性化后 IC / Newey-West 修正，自己实现反而更可控。

核心指标
--------
* **IC**：因子值与前瞻收益的 Pearson 相关（逐日截面）；
* **RankIC**：Spearman 相关（对极值稳健，A 股实操中更常用）；
* **ICIR**：IC 均值 / IC 标准差 —— 衡量稳定性，>0.3 算可用；
* **t 值**：默认做 **Newey-West 修正**，因为持有期重叠会让 IC 序列自相关，
  普通 t 值会系统性高估显著性（这是新手最容易犯的错）；
* **分位数收益**：按因子分 10 档，看多空收益与单调性；
* **换手率**：因子自身的一阶自相关，太低意味着每天换一批票，成本吃光 alpha。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd
from scipy import stats

TRADING_DAYS = 242


# ---------------------------------------------------------------------------
# 分组内相关系数（全表向量化，避免逐日 apply）
# ---------------------------------------------------------------------------


def _group_corr(x: pd.Series, y: pd.Series, key: pd.Series) -> pd.Series:
    """按 key 分组计算 x, y 的 Pearson 相关，返回 index=分组值的 Series。

    注意：``r`` 的索引是**分组键**，而 transform 出来的结果是**行索引**，
    两者不能直接混用（``.where`` 会按索引对齐，错位即全 NaN）——
    所以组内计数要单独用 ``groupby().size()`` 拿，索引才是分组键。
    """
    gx = x.groupby(key)
    mx = gx.transform("mean")
    my = y.groupby(key).transform("mean")
    dx, dy = x - mx, y - my
    num = (dx * dy).groupby(key).sum()
    vx = (dx * dx).groupby(key).sum()
    vy = (dy * dy).groupby(key).sum()
    den = np.sqrt(vx * vy)
    r = num / den.replace(0.0, np.nan)
    cnt = gx.size()  # index = 分组键
    return r.where(cnt >= 3)


def _rank_within(x: pd.Series, key: pd.Series) -> pd.Series:
    """分组内百分比排名（等价于 Spearman 的秩）。"""
    return x.groupby(key).rank(pct=True)


def newey_west_t(series: pd.Series, lags: int | None = None) -> tuple[float, float]:
    """Newey-West 修正的 t 值与 p 值。

    重叠持有期（如 fwd_ret_5）会让 IC 序列高度自相关，
    普通 t 检验的 i.i.d. 假设失效，t 值虚高。
    """
    s = pd.Series(series).dropna()
    n = len(s)
    if n < 5:
        return float("nan"), float("nan")
    mu = float(s.mean())
    if lags is None:
        lags = int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0)))
    lags = max(0, min(lags, n - 2))

    e = s - mu
    gamma0 = float((e * e).sum()) / n
    var = gamma0
    for l in range(1, lags + 1):
        gl = float((e.iloc[l:].to_numpy() * e.iloc[:-l].to_numpy()).sum()) / n
        var += 2.0 * (1.0 - l / (lags + 1.0)) * gl
    if var <= 0:
        return float("nan"), float("nan")
    se = math.sqrt(var / n)
    if se == 0:
        return float("nan"), float("nan")
    t = mu / se
    p = 2.0 * (1.0 - stats.t.cdf(abs(t), df=n - 1))
    return float(t), float(p)


# ---------------------------------------------------------------------------
# 单因子检验
# ---------------------------------------------------------------------------


@dataclass
class FactorIC:
    name: str
    n_days: int = 0
    avg_cross: float = 0.0
    ic_mean: float = float("nan")
    ic_std: float = float("nan")
    icir: float = float("nan")
    ic_t: float = float("nan")
    ic_p: float = float("nan")
    rank_ic_mean: float = float("nan")
    rank_ic_std: float = float("nan")
    rank_icir: float = float("nan")
    rank_ic_t: float = float("nan")
    rank_ic_p: float = float("nan")
    pos_ratio: float = float("nan")
    autocorr: float = float("nan")     # 因子自相关（换手代理）
    turnover: float = float("nan")     # 分位组变动比例
    q_ls: float = float("nan")         # 多空年化收益
    q_monotonic: float = float("nan")  # 分位收益单调性
    q_returns: list[float] = None      # type: ignore[assignment]
    direction: int = 1                 # 实测有效方向
    decay: dict[str, float] = None     # type: ignore[assignment]

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


def compute_ic_series(
    df: pd.DataFrame,
    factor_cols: list[str],
    ret_col: str = "fwd_ret_1",
    date_col: str = "date",
    min_cross: int = 50,
    tradable_col: str | None = "t1_tradable",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """逐日计算 IC / RankIC。

    返回
    ----
    ic_ts : DataFrame，index=date，列 = {factor}__ic / {factor}__rankic / {factor}__n
    detail : 未使用（保留扩展）
    """
    d = df
    if tradable_col and tradable_col in d.columns:
        d = d[d[tradable_col].astype(bool)]
    d = d[[date_col, ret_col] + [c for c in factor_cols if c in d.columns]].copy()
    d = d.dropna(subset=[ret_col])

    key = d[date_col]
    n_by_day = d.groupby(date_col)[ret_col].transform("size")
    d = d[n_by_day >= min_cross]
    key = d[date_col]

    ret = d[ret_col].astype("float64")
    ret_rank = _rank_within(ret, key)

    out: dict[str, pd.Series] = {}
    for c in factor_cols:
        if c not in d.columns:
            continue
        x = d[c].astype("float64")
        ok = x.notna()
        xx, rr, kk = x[ok], ret[ok], key[ok]
        out[f"{c}__ic"] = _group_corr(xx, rr, kk)
        xr = _rank_within(xx, kk)
        rr2 = _rank_within(rr, kk)
        out[f"{c}__rankic"] = _group_corr(xr, rr2, kk)
        out[f"{c}__n"] = xx.groupby(kk).size()

    ic_ts = pd.DataFrame(out)
    ic_ts.index.name = date_col
    return ic_ts, pd.DataFrame()


def summarize_ic(
    ic_ts: pd.DataFrame,
    factor_cols: list[str],
    horizon: int = 1,
    nw_lags: int | None = None,
) -> pd.DataFrame:
    """把 IC 时序汇总成每个因子一行。"""
    rows = []
    for c in factor_cols:
        ic = ic_ts.get(f"{c}__ic")
        ric = ic_ts.get(f"{c}__rankic")
        if ic is None or ric is None:
            continue
        ic = ic.dropna()
        ric = ric.dropna()
        if len(ric) < 20:
            continue
        t, p = newey_west_t(ric, lags=nw_lags if nw_lags is not None else max(horizon, 4))
        it, ip = newey_west_t(ic, lags=nw_lags if nw_lags is not None else max(horizon, 4))
        rm, rs = float(ric.mean()), float(ric.std(ddof=1))
        im, isd = float(ic.mean()), float(ic.std(ddof=1))
        rows.append(
            {
                "factor": c,
                "n_days": int(len(ric)),
                "rank_ic": rm,
                "rank_ic_std": rs,
                "rank_icir": rm / rs if rs else float("nan"),
                "rank_ic_t": t,
                "rank_ic_p": p,
                "ic": im,
                "ic_std": isd,
                "icir": im / isd if isd else float("nan"),
                "ic_t": it,
                "ic_p": ip,
                "pos_ratio": float((ric > 0).mean()),
                "direction": 1 if rm >= 0 else -1,
            }
        )
    return pd.DataFrame(rows).sort_values("rank_icir", key=lambda s: s.abs(), ascending=False)


# ---------------------------------------------------------------------------
# 分位数检验
# ---------------------------------------------------------------------------


def quantile_returns(
    df: pd.DataFrame,
    factor: str,
    ret_col: str = "fwd_ret_1",
    date_col: str = "date",
    n_q: int = 10,
    horizon: int = 1,
    tradable_col: str | None = "t1_tradable",
    min_cross: int = 50,
) -> dict:
    """按因子分 n_q 档，算每档年化收益与单调性。"""
    d = df
    if tradable_col and tradable_col in d.columns:
        d = d[d[tradable_col].astype(bool)]
    # symbol 必须一起带上，否则换手率（依赖个股前后两天的分档对比）算不出来
    need = [date_col, factor, ret_col] + (["symbol"] if "symbol" in df.columns else [])
    d = d[need].dropna(subset=[factor, ret_col])
    cnt = d.groupby(date_col)[factor].transform("size")
    d = d[cnt >= min_cross]
    if d.empty:
        return {"q_returns": [], "long_short": float("nan"), "monotonic": float("nan"),
                "turnover": float("nan")}

    # 逐日分档（用 rank 分位，天然处理并列值）
    rk = d.groupby(date_col)[factor].rank(pct=True)
    d = d.assign(_q=np.minimum((rk * n_q).astype(int) + 1, n_q))

    per_day = d.groupby([date_col, "_q"])[ret_col].mean().unstack("_q")
    # 年化：先用持有期折算成日收益，再 ×242
    ann = (per_day.mean() / horizon) * TRADING_DAYS
    q_returns = [float(ann.get(i, float("nan"))) for i in range(1, n_q + 1)]

    long_short = float(ann.get(n_q, float("nan")) - ann.get(1, float("nan")))

    # 单调性：档位序号 vs 档位收益 的 Spearman
    idx = np.arange(1, n_q + 1)
    vals = np.array(q_returns, dtype="float64")
    ok = np.isfinite(vals)
    monotonic = float(stats.spearmanr(idx[ok], vals[ok]).statistic) if ok.sum() >= 3 else float("nan")

    # 换手率：相邻两日分档发生变化的比例（只看首尾档，即真正会被交易的票）
    # 注意：shift 前必须按 (symbol, date) 排序，按 (factor, date) 排序会让
    # shift(1) 取到"同一天的另一只票"，算出来的换手率是垃圾（曾全为 NaN）。
    turnover = float("nan")
    if "symbol" in d.columns:
        dd = d.sort_values(["symbol", date_col])
        prev_q = dd.groupby("symbol")["_q"].shift(1)
        head = dd["_q"].isin([1, n_q])
        if head.any():
            turnover = float(1.0 - (prev_q[head] == dd["_q"][head]).mean())

    # 因子自相关（更稳的换手代理）：逐日 spearman(f_t, f_{t-1})
    ac = _factor_autocorr(df, factor, date_col=date_col)

    return {
        "q_returns": q_returns,
        "long_short": long_short,
        "monotonic": monotonic,
        "turnover": turnover,
        "autocorr": ac,
    }


def _factor_autocorr(df: pd.DataFrame, factor: str, date_col: str = "date") -> float:
    """因子截面值的日间自相关（Spearman 秩自相关，取所有交易日的中位数）。"""
    if "symbol" not in df.columns:
        return float("nan")
    d = df[[date_col, "symbol", factor]].dropna()
    if d.empty:
        return float("nan")
    d = d.sort_values(["symbol", date_col])
    rk = d.groupby(date_col)[factor].rank(pct=True)
    d = d.assign(_r=rk)
    d["_r_prev"] = d.groupby("symbol")["_r"].shift(1)
    d = d.dropna(subset=["_r", "_r_prev"])
    acs = d.groupby(date_col).apply(
        lambda g: float(stats.spearmanr(g["_r"], g["_r_prev"]).statistic)
        if len(g) >= 10 else float("nan"),
        include_groups=False,
    )
    return float(acs.median()) if len(acs.dropna()) else float("nan")


# ---------------------------------------------------------------------------
# 因子相关性矩阵
# ---------------------------------------------------------------------------


def factor_correlation(
    df: pd.DataFrame,
    factor_cols: list[str],
    date_col: str = "date",
    method: str = "spearman",
    min_cross: int = 50,
) -> pd.DataFrame:
    """因子间的横截面相关性（逐日算再取均值，比全样本 pooling 更能反映真实共线性）。"""
    cols = [c for c in factor_cols if c in df.columns]
    d = df[[date_col] + cols].copy()
    cnt = d.groupby(date_col)[cols[0]].transform("size")
    d = d[cnt >= min_cross]
    if d.empty:
        return pd.DataFrame()

    if method == "spearman":
        for c in cols:
            d[c] = d.groupby(date_col)[c].rank(pct=True)

    cors = []
    key = d[date_col]
    for _, g in d.groupby(date_col, sort=False):
        sub = g[cols]
        if len(sub) < min_cross:
            continue
        cors.append(sub.corr())
    if not cors:
        return pd.DataFrame()
    return pd.concat(cors).groupby(level=0).mean().reindex(index=cols, columns=cols)


def redundant_pairs(corr: pd.DataFrame, threshold: float = 0.7) -> list[tuple[str, str, float]]:
    """找出高相关因子对（|ρ| >= threshold）。"""
    if corr.empty:
        return []
    out = []
    cols = list(corr.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            v = corr.loc[a, b]
            if pd.notna(v) and abs(v) >= threshold:
                out.append((a, b, float(v)))
    return sorted(out, key=lambda x: -abs(x[2]))


def select_factors(
    summary: pd.DataFrame,
    corr: pd.DataFrame,
    min_abs_ic: float = 0.01,
    max_p: float = 0.05,
    corr_threshold: float = 0.85,
    min_neg_icir_keep: float = 0.5,
    name_col: str = "factor",
    icir_col: str = "rank_icir",
    ic_col: str = "rank_ic",
    p_col: str = "rank_ic_p",
) -> tuple[list[str], dict[str, list]]:
    """选因子：显著性门控 → 中性化抗性检查 → 相关性去冗余。

    这是 IC 加权定权前的**必要**步骤。少了它会出现两个典型故障：

    1. **过拟合**：把 20+ 个弱因子全塞进组合，等于拿噪声加权平均；
    2. **单信息重复计权**：ρ=0.95 的一对因子各拿一份权重，组合被无形押注在
       同一条逻辑上；方向相反的一对（如 mom_20 / rev_20，ρ=-1）甚至会互相抵消。

    参数
    ----
    min_abs_ic / max_p
        显著性门控：\\|RankIC\\| >= min_abs_ic 且 p <= max_p 才保留。
    min_neg_icir_keep
        **中性化抗性**：若某因子在 ``summary`` 里带 ``icir_neu`` 列，且
        \\|icir_neu\\| / \\|icir_raw\\| < 该比例，说明其预测力主要来自行业/市值暴露，
        予以剔除。典型受害者是涨停次数因子（比值仅 0.22）。
    corr_threshold
        相关性去冗余阈值，按 \\|RankICIR\\| 从强到弱贪心保留。

    返回 ``(入选列表, 剔除明细 dict)``。
    """
    if name_col not in summary.columns or summary.empty:
        return [], {}
    df = summary.copy()
    keep_mask = (df[ic_col].abs() >= min_abs_ic) & (df[p_col] <= max_p)

    rejected: dict[str, list] = {"不显著": [], "风格依赖": [], "冗余": []}
    for nm, ok in zip(df[name_col], keep_mask):
        if not ok:
            rejected["不显著"].append(str(nm))
    sig = df[keep_mask].copy()

    # 中性化抗性
    if icir_col in sig.columns:
        neu_col = next((c for c in ("icir_neu", "rank_icir_neu") if c in sig.columns), None)
        if neu_col:
            # ⚠️ 分母必须是**未中性化**的 ICIR —— 本函数 docstring 写的就是
            # |icir_neu| / |icir_raw|，但早期实现用了 icir_col（= rank_icir）。
            # 在主口径 `full_neu*` 变体下 rank_icir ≡ icir_neu，比值恒等于 1，
            # 于是**这道门从未生效过**；只有在 `full_raw` 变体下才恰好是对的。
            # 换成 icir_raw 后立刻抓到 limit_up_cnt_20（比值 0.184）——
            # 正是 docstring 里点名的那类因子：预测力几乎全部来自风格暴露。
            raw_col = next((c for c in ("icir_raw", "rank_icir_raw") if c in sig.columns),
                           icir_col)
            ratio = sig[neu_col].abs() / sig[raw_col].abs().replace(0, np.nan)
            style_dep = ratio < min_neg_icir_keep
            for nm in sig.loc[style_dep, name_col]:
                rejected["风格依赖"].append(str(nm))
            sig = sig[~style_dep]

    # 相关性去冗余
    keep, dropped = prune_redundant(sig, corr, threshold=corr_threshold, name_col=name_col)
    for nm, because, rho in dropped:
        rejected["冗余"].append(f"{nm}（与 {because} ρ={rho:+.3f}）")
    return keep, rejected


def prune_redundant(
    summary: pd.DataFrame,
    corr: pd.DataFrame,
    threshold: float = 0.85,
    name_col: str = "factor",
) -> tuple[list[str], list[tuple[str, str, float]]]:
    """贪心去冗余：按 |RankICIR| 从强到弱保留，剔除与已保留因子高相关的。

    为什么必须做：IC 加权给两个 ρ=0.95 的因子各分一份权重，等于把同一条信息
    算了两遍，组合会无意识地重仓在单一逻辑上；更糟的是方向相反的一对
    （如 mom_20 与 rev_20，ρ=-1）会互相抵消或莫名放大。

    返回 ``(保留列表, [(被剔除, 因与谁相关, ρ), ...])``。
    """
    if corr.empty or name_col not in summary.columns:
        return list(summary.get(name_col, [])), []
    order = summary.sort_values("rank_icir", key=lambda s: s.abs(), ascending=False)
    keep: list[str] = []
    dropped: list[tuple[str, str, float]] = []
    for name in order[name_col]:
        nm = str(name)
        conflict = None
        for k in keep:
            if nm in corr.index and k in corr.columns:
                v = corr.loc[nm, k]
                if pd.notna(v) and abs(v) >= threshold:
                    conflict = (nm, k, float(v))
                    break
        if conflict:
            dropped.append(conflict)
        else:
            keep.append(nm)
    return keep, dropped


# ---------------------------------------------------------------------------
# 一键：完整因子报告
# ---------------------------------------------------------------------------


def factor_report(
    df: pd.DataFrame,
    factor_cols: list[str],
    ret_cols: dict[int, str] | None = None,
    date_col: str = "date",
    n_q: int = 10,
    min_cross: int = 50,
    tradable_col: str | None = "t1_tradable",
    with_quantile: bool = True,
) -> dict:
    """跑完整套检验，返回 dict(summary=, ic_ts=, quantile=, corr=, decay=)。"""
    ret_cols = ret_cols or {1: "fwd_ret_1", 5: "fwd_ret_5", 10: "fwd_ret_10", 20: "fwd_ret_20"}
    ret_cols = {h: c for h, c in ret_cols.items() if c in df.columns}
    if not ret_cols:
        raise ValueError("面板中没有前瞻收益列，请检查 horizons 配置")

    base_h = min(ret_cols)
    base_col = ret_cols[base_h]

    ic_ts, _ = compute_ic_series(
        df, factor_cols, ret_col=base_col, date_col=date_col,
        min_cross=min_cross, tradable_col=tradable_col,
    )
    summary = summarize_ic(ic_ts, factor_cols, horizon=base_h)

    # IC 衰减：各持有期的 rank_ic 均值
    decay: dict[str, dict[str, float]] = {}
    for h, col in sorted(ret_cols.items()):
        ts, _ = compute_ic_series(
            df, factor_cols, ret_col=col, date_col=date_col,
            min_cross=min_cross, tradable_col=tradable_col,
        )
        decay[f"h{h}"] = {c: float(ts[f"{c}__rankic"].dropna().mean())
                          for c in factor_cols if f"{c}__rankic" in ts}

    # 分位数
    quant: dict[str, dict] = {}
    if with_quantile:
        for c in factor_cols:
            quant[c] = quantile_returns(
                df, c, ret_col=base_col, date_col=date_col, n_q=n_q,
                horizon=base_h, tradable_col=tradable_col, min_cross=min_cross,
            )
        summary["q_ls"] = summary["factor"].map(lambda c: quant.get(c, {}).get("long_short", np.nan))
        summary["q_mono"] = summary["factor"].map(lambda c: quant.get(c, {}).get("monotonic", np.nan))
        summary["turnover"] = summary["factor"].map(lambda c: quant.get(c, {}).get("turnover", np.nan))
        summary["autocorr"] = summary["factor"].map(lambda c: quant.get(c, {}).get("autocorr", np.nan))
        for h in sorted(ret_cols):
            summary[f"ic_h{h}"] = summary["factor"].map(lambda c: decay[f"h{h}"].get(c, np.nan))

    corr = factor_correlation(df, factor_cols, date_col=date_col, min_cross=min_cross)

    return {
        "summary": summary,
        "ic_ts": ic_ts,
        "quantile": quant,
        "corr": corr,
        "decay": decay,
        "base_horizon": base_h,
    }
