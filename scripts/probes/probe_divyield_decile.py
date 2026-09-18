# -*- coding: utf-8 -*-
"""阶段 B 第一问：**高股息选股在「无偏样本」上到底有没有效？**

背景
----
`docs/终局诊断与重启方案.md` §4 把阶段 B 定为「B3 红利低波」，零假设 =
直接买中证红利 ETF（已在 `probe_dividend_etf_baseline.py` 量化：相对沪深300
超额年化 −0.66% ~ +10.10%，\|t\| ≤ 0.63，不显著）。
本探针回答的是**另一半**：不用 ETF、直接在**股票层面按股息率选股**能不能跑出来。

⚠️ 为什么必须用无偏样本（本项目反复强调，别省这一步）
--------------------------------------------------
§1.4 已证明"便宜 ≈ 跌得多"，而**股息率陷阱**（股价崩了导致股息率虚高）
只有在把退市股并回来之后才会现原形。用只含现役的有偏样本测红利 =
一定测出假 alpha。本探针的池 = 现役 `bars/` ∪ 退市 `delisted_bars/`。

口径（改动前必读）
------------------
1. **真实价** = ``close / adj_factor``（bars 的 close 是后复权价，别再乘）。
2. **持有期收益** = 后复权 close 的比值 − 1（复权价自带分红/送股，正确）。
3. **PIT 股息率**：调仓日 d 的每股年派现 = Σ 每股派现，取
   ``ex_div_date ∈ (d−365, d]``；股息率 = 年派现 / 真实价(d)。
   ⛔ **绝不能按「年报年度」对齐到年初用** —— `fetch_dividend_yield.py` 自检实测
   **99.8% 的分红预案在次年才公告**，按年度对齐会系统性前视约半年。
   每股派现 = ``cash_per10 / 10``（源列是每 10 股派现，单位锚已验）。
4. 过滤：上市满 180 天 + 近 20 日日均成交额 ≥ 2e7（liquid 口径，与主口径一致）。
5. 调仓：每 21 个交易日，档内等权，下一次调仓卖出。

⚠️ 已知偏差（如实登记，别当已解决）
------------------------------------
**未施加「退市可实现损失」**：退市股末行即退市日，本探针按**最后价格离场**，
不计退市整理期后的损失（`probe_survivorship_corrected.py` 实测退市股 forward
收益中位 −81.9%）。
⇒ 偏差方向：**高股息档里"落下的刀子"更多，不计损失会系统性高估高股息档**。
⇒ 因此 **本探针若得出"高股息无效"，该结论是稳健的**（偏差方向对结论不利）；
   反之若得出"有效"，必须先补上退市损失才能引用。

用法
----
    python scripts/probes/probe_divyield_decile.py
    python scripts/probes/probe_divyield_decile.py --start 2019-01-01 --rebalance 21

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

from aq.data.etf_store import PROJECT_ROOT  # noqa: E402

BARS = PROJECT_ROOT / "data_cache" / "bars"
DELISTED = PROJECT_ROOT / "data_cache" / "delisted_bars"
DIV = PROJECT_ROOT / "data_cache" / "dividend_yield.parquet"

TRADING_DAYS = 252
N_DECILES = 10


def load_div_pit() -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """每只股票的 (除权除息日数组, 每股派现累计数组) —— 供 trailing 求和用。"""
    df = pd.read_parquet(DIV)
    df = df[df["ex_div_date"].notna() & (df["cash_per10"] > 0)].copy()
    df["cash_ps"] = df["cash_per10"] / 10.0
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for code, g in df.groupby("code"):
        g = g.sort_values("ex_div_date")
        d = g["ex_div_date"].values.astype("datetime64[ns]")
        cum = np.concatenate([[0.0], np.cumsum(g["cash_ps"].values)])
        out[code] = (d, cum)
    return out


def collect(start: str, end: str | None, reb_days: int,
            min_amount: float, min_age: int):
    """扫全池，产出 (调仓日 × 股票) 的「股息率」与「下期收益」两张表。"""
    # 交易日历：用 000001 的交易日
    ref = pd.read_parquet(BARS / "000001.parquet")
    ref["time"] = pd.to_datetime(ref["time"])
    cal = ref.loc[(ref["time"] >= pd.Timestamp(start)), "time"]
    if end:
        cal = cal[cal <= pd.Timestamp(end)]
    cal = cal.sort_values().reset_index(drop=True)
    reb = pd.DatetimeIndex(cal.iloc[::reb_days].values)
    print(f"交易日历      : {len(cal)} 天（{cal.iloc[0].date()} ~ {cal.iloc[-1].date()}）")
    print(f"调仓日        : {len(reb)} 个（每 {reb_days} 交易日）")

    div = load_div_pit()
    print(f"有除权除息记录: {len(div)} 只")

    files = [(BARS, p) for p in sorted(BARS.glob("*.parquet"))] + \
            [(DELISTED, p) for p in sorted(DELISTED.glob("*.parquet"))]
    print(f"待扫文件      : {len(files)} 只（现役 {len(list(BARS.glob('*.parquet')))} "
          f"+ 退市 {len(list(DELISTED.glob('*.parquet')))}）")

    dy_rows, ret_rows = {}, {}
    n_ok = 0
    for base, p in files:
        code = p.stem.split(".")[0]
        try:
            df = pd.read_parquet(p)
            if len(df) < min_age + 5:
                continue
            df["time"] = pd.to_datetime(df["time"])
            df = df.sort_values("time").set_index("time")
            df = df[~df.index.duplicated(keep="last")]
            sub = df.loc[df.index >= pd.Timestamp(start)]
            if end:
                sub = sub.loc[sub.index <= pd.Timestamp(end)]
            if len(sub) < 30:
                continue
            # 对齐到调仓日（ffill）
            al = sub.reindex(reb, method="ffill")
            close_hfq = al["close"]
            px = close_hfq / al["adj_factor"]
            amt20 = sub["amount"].rolling(20).mean().reindex(reb, method="ffill")
            age = pd.Series(np.arange(len(sub)), index=sub.index).reindex(reb, method="ffill")
            # 下期收益：下一调仓日的后复权价 / 本期 − 1
            nxt = close_hfq.shift(-1)
            # 退市股：最后一个真实日后不再有价 → 用最后真实价离场
            last_ts = sub.index[-1]
            beyond = reb > last_ts
            if beyond.any():
                nxt = nxt.copy()
                nxt[beyond] = np.nan
                # 最后一个仍在期内的调仓日：以最后真实价离场
                last_real_close = float(sub["close"].iloc[-1])
                inside = reb[~beyond]
                nxt.loc[inside[-1]] = last_real_close
            ret = nxt / close_hfq - 1.0
            ok = (close_hfq.notna() & ret.notna() & px.notna()
                  & (amt20 >= min_amount) & (age >= min_age))
            if not ok.any():
                continue
            # PIT 股息率：过去 365 天已除权除息的每股派现 / 真实价
            if code in div:
                exd, cum = div[code]
                lo = np.searchsorted(exd, (reb - pd.Timedelta(days=365)).values, side="right")
                hi = np.searchsorted(exd, reb.values, side="right")
                cash = pd.Series(cum[hi] - cum[lo], index=reb)
            else:
                cash = pd.Series(0.0, index=reb)
            dyy = (cash / px).where(ok & (cash > 0))
            ret_rows[code] = ret.where(ok)
            dy_rows[code] = dyy
            n_ok += 1
        except Exception:
            continue

    print(f"进入面板      : {n_ok} 只")
    dy = pd.DataFrame(dy_rows)
    rt = pd.DataFrame(ret_rows)
    return dy, rt, reb


def metrics(r: pd.Series, rf_daily: float) -> dict:
    r = r.dropna()
    n = len(r)
    if n < 5:
        return {}
    years = n / (TRADING_DAYS / 21.0)  # 每期 21 交易日
    growth = float((1.0 + r).prod())
    cagr = growth ** (1.0 / years) - 1.0 if years > 0 else float("nan")
    sd = float(r.std(ddof=1))
    per = 21.0
    vol = sd * np.sqrt(TRADING_DAYS / per)
    mu = float(r.mean())
    sharpe = ((mu - rf_daily * per) / sd * np.sqrt(TRADING_DAYS / per)) if sd > 0 else float("nan")
    curve = (1.0 + r).cumprod()
    mdd = float((curve / curve.cummax() - 1.0).min())
    return {"n": n, "years": years, "cagr": cagr, "vol": vol,
            "sharpe": sharpe, "max_dd": mdd, "mean_per": mu}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--rebalance", type=int, default=21)
    ap.add_argument("--min-amount", type=float, default=2e7)
    ap.add_argument("--min-age", type=int, default=180)
    args = ap.parse_args()

    rf_daily = 0.02 / TRADING_DAYS
    print("=" * 84)
    print("阶段 B：高股息选股分档检验（无偏池 = 现役 ∪ 退市）")
    print("=" * 84)

    dy, rt, reb = collect(args.start, args.end, args.rebalance,
                          args.min_amount, args.min_age)
    if dy.shape[1] < 50:
        print("!! 样本太少")
        return 1

    # 逐期分档
    dec_ret = {i: [] for i in range(1, N_DECILES + 1)}
    eq_ret, dy_med = [], []
    n_sel = []
    for d in dy.index:
        row = dy.loc[d].dropna()
        r = rt.loc[d].reindex(row.index).dropna()
        common = row.index.intersection(r.index)
        if len(common) < 100:
            continue
        row, r = row[common], r[common]
        try:
            bins = pd.qcut(row.rank(method="first"), N_DECILES, labels=False) + 1
        except ValueError:
            continue
        for i in range(1, N_DECILES + 1):
            m = bins == i
            if m.any():
                dec_ret[i].append(float(r[m].mean()))
        eq_ret.append(float(r.mean()))
        dy_med.append(float(row.median()))
        n_sel.append(len(common))

    print(f"有效调仓期    : {len(eq_ret)} 期（每期入选 {int(np.mean(n_sel))} 只，"
          f"中位股息率 {np.mean(dy_med):.2%}）")
    if len(eq_ret) < 10:
        print("!! 期数太少")
        return 1

    eq = pd.Series(eq_ret)
    print()
    hdr = (f"{'档':<6}{'年化':>9}{'波动':>9}{'夏普':>8}{'最大回撤':>10}"
           f"{'超额年化':>10}{'t(超额)':>9}")
    print(hdr)
    print("-" * len(hdr))
    m_eq = metrics(eq, rf_daily)
    print(f"{'全池等权':<6}{m_eq['cagr']:>9.2%}{m_eq['vol']:>9.2%}{m_eq['sharpe']:>8.2f}"
          f"{m_eq['max_dd']:>10.2%}{0.0:>10.2%}{0.0:>9.2f}")
    print("-" * len(hdr))

    res = {}
    for i in range(1, N_DECILES + 1):
        s = pd.Series(dec_ret[i])
        m = metrics(s, rf_daily)
        if not m:
            continue
        ex = s - eq.values[:len(s)]
        sd = float(ex.std(ddof=1))
        # 每期 21 交易日 → 年化因子 = 252/21
        t = (float(ex.mean()) / (sd / np.sqrt(len(ex)))) if sd > 0 else float("nan")
        print(f"{'D' + str(i):<6}{m['cagr']:>9.2%}{m['vol']:>9.2%}{m['sharpe']:>8.2f}"
              f"{m['max_dd']:>10.2%}{m['cagr'] - m_eq['cagr']:>10.2%}{t:>9.2f}")
        res[i] = (m, t)

    if N_DECILES in res and 1 in res:
        hi, lo = res[N_DECILES], res[1]
        print("-" * len(hdr))
        print(f"D{N_DECILES} − D1 年化差 : {hi[0]['cagr'] - lo[0]['cagr']:+.2%}")
        s_hi = pd.Series(dec_ret[N_DECILES])
        s_lo = pd.Series(dec_ret[1])
        k = min(len(s_hi), len(s_lo))
        d = pd.Series(s_hi.values[:k] - s_lo.values[:k])
        sd = float(d.std(ddof=1))
        t_ls = float(d.mean()) / (sd / np.sqrt(k)) if sd > 0 else float("nan")
        print(f"多空 t            : {t_ls:+.2f}（{k} 期，年化因子 {TRADING_DAYS / args.rebalance:.1f}）")
        print(f"t=2 需要          : |t| ≥ 2；本样本 {m_eq['years']:.1f} 年")

        # 审计：多空序列的逐期分布 —— t 若小，必须能看出是"噪声大"还是"被孤点毁了"
        af = TRADING_DAYS / args.rebalance
        print()
        print("[审计] 多空逐期价差（D10 − D1）：")
        print(f"    期数 {len(d)} | 均值 {d.mean():+.3%} | 标准差 {sd:.3%} "
              f"（年化 {sd * np.sqrt(af):.2%}）")
        print(f"    最小 {d.min():+.2%} | 中位 {d.median():+.3%} | 最大 {d.max():+.2%}")
        q = d.abs().nlargest(3)
        print(f"    绝对值最大的 3 期："
              + "、".join(f"第{int(i)}期 {d[i]:+.2%}" for i in q.index))
        # 去掉最极端 1 期后再算一次：若 t 暴涨 → 结论被孤点绑架，不可引用
        d2 = d.drop(index=d.abs().idxmax())
        sd2 = float(d2.std(ddof=1))
        t2 = float(d2.mean()) / (sd2 / np.sqrt(len(d2))) if sd2 > 0 else float("nan")
        print(f"    剔除最极端 1 期后 t = {t2:+.2f}（原 {t_ls:+.2f}）"
              f" → {'⚠️ t 对孤点敏感，结论不可引用' if abs(t2 - t_ls) > 0.5 else 't 对孤点不敏感'}")

    print()
    print("⚠️ 已知偏差：**未施加退市可实现损失**（退市股按最后价格离场）。")
    print("   偏差方向 = 高估高股息档 ⇒ 若结论为「无效」，结论稳健；")
    print("   若结论为「有效」，必须先补退市损失才可引用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
