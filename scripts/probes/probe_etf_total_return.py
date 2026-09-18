# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只拉外部行情/分红做口径对照。
# 2026-09-18 新建（纳入版本控制），让「分红拖累 = ?pp/年」这个数字可被外部复现。
# 前置：需要 akshare；不需要先有 data_cache/。
# ---------------------------------------------------------------------------
"""ETF **全收益口径** 对照（阶段 1a-4，只读）。

为什么这个口径是决定性的，不是细节
----------------------------------
`docs/小额实盘方案与判据.md` §1.2 用**价格指数**算出「回撤 ≤10% ⇒ 权益 25%
⇒ 组合年化 2.80%，含股息约 3.4%」。而本探针实测的分红拖累是 **1.7~3.2pp/年** ——
**量级等于整个组合的预期收益**。口径选错，等于把结论本身改掉。

三个必须分开的口径
------------------
1. **价格**（腾讯/新浪 `close`，不复权）—— 用于**撮合与估值**，这是唯一能成交的价。
2. **全收益**（价格 + 分红再投）—— 用于**收益归因与仓位标定**。
   ⚠️ 东财的「累计净值」**不能**当复权因子：510300 实测单位净值 3.0278 vs
   累计净值 1.2226（差 2.5 倍），那是**份额折算**不是分红。
3. **前/后复权**（`stock_zh_a_hist_tx(adjust=...)`）—— 实测**不可信**：
   510050 三档比例互相矛盾（默认 3.38x / qfq 37.4x / hfq 4.29x），故不用。

全收益的构造（分红不再投资口径）
--------------------------------
设收盘价 ``P_t``、累计分红 ``D_t``（新浪 `fund_etf_dividend_sina` 的
「累计分红」，单位与价格同为元／份）：

    r_t = (P_t + D_t − D_{t−1}) / P_{t−1} − 1

分红在**除息日**计入。这是"拿到现金不再投"的口径，比"分红再投"保守一点点。

用法::

    python scripts/probes/probe_etf_total_return.py
    python scripts/probes/probe_etf_total_return.py --codes sh510880 sh511010
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

#: 核心篮子：宽基 + 红利 + 红利低波 + 债券 + 黄金 + 创业板。
#: 债券（511010 国债ETF）特别重要 —— 它是我们"现金/短债"那一档的**实际**收益，
#: 阶段 0 那里用的是 rf = 2%/年 的**假设值**，这里可以用真实数据替换。
CORE_BASKET = [
    ("sh510050", "上证50ETF", "宽基"),
    ("sh510300", "沪深300ETF", "宽基"),
    ("sh510500", "中证500ETF", "宽基"),
    ("sz159915", "创业板ETF", "宽基"),
    ("sh588000", "科创50ETF", "宽基"),
    ("sh510880", "上证红利ETF", "红利"),
    ("sh512890", "红利低波ETF", "红利"),
    ("sh511010", "国债ETF", "债券"),
    ("sh518880", "黄金ETF", "商品"),
    ("sh513100", "纳指ETF", "跨境"),
]

#: 与阶段 0 对齐的窗口（`docs/小额实盘方案与判据.md` §1 用的就是它）
STAGE0_START, STAGE0_END = "2019-01-01", "2026-09-15"

TRADING_DAYS = 242


def _probe(fn, timeout: float = 70.0):
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["v"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["e"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_worker)
    t.daemon = True
    t.start()
    t.join(timeout)
    if t.is_alive():
        return ("timeout", None)
    if "e" in box:
        return ("error", box["e"])
    return ("ok", box["v"])


# ---------------------------------------------------------------------------
# 数据与口径
# ---------------------------------------------------------------------------

def load_price(sym: str):
    """腾讯源日线（唯一能回溯到 2005 的免费源之一）。"""
    import akshare as ak

    df = ak.stock_zh_a_hist_tx(symbol=sym, start_date="20040101", end_date="20260915")
    import pandas as pd

    df["time"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df[["time", "close"]].dropna().sort_values("time").reset_index(drop=True)


def load_dividend(sym: str):
    """新浪逐只 ETF 分红（「累计分红」= 每份累计派现金额，单位元）。"""
    import akshare as ak
    import pandas as pd

    df = ak.fund_etf_dividend_sina(symbol=sym)
    df["time"] = pd.to_datetime(df["日期"])
    df["cum_div"] = pd.to_numeric(df["累计分红"], errors="coerce")
    return df[["time", "cum_div"]].dropna().sort_values("time").reset_index(drop=True)


def total_return_series(px, dv):
    """构造全收益日序列。返回 (价格序列, 全收益序列)，均为 date 索引。"""
    import pandas as pd

    p = px.set_index("time")["close"].astype(float)
    if dv is None or len(dv) == 0:
        return p, p.rename("total")
    d = dv.set_index("time")["cum_div"].astype(float)
    d = d[~d.index.duplicated(keep="last")].reindex(p.index, method="ffill").fillna(0.0)
    r_tot = ((p + d.diff().fillna(0.0)) / p.shift(1) - 1.0).dropna()
    trip = (1.0 + r_tot).cumprod()
    trip = trip / trip.iloc[0]
    return p, trip.rename("total")


def _ann(r) -> float:
    """日收益序列 → 年化。"""
    n = len(r)
    if n < 2:
        return float("nan")
    return float((1.0 + r).prod() ** (TRADING_DAYS / n) - 1.0)


def _max_dd(series) -> tuple[float, str, str]:
    """最大回撤（返回负值、峰日、谷日）。watermark 口径。"""
    import numpy as np

    nav = series.astype(float)
    peak = nav.cummax()
    dd = nav / peak - 1.0
    i = int(np.argmin(dd.values))
    j = int(np.argmax((nav.iloc[: i + 1]).values))
    return (float(dd.iloc[i]), str(nav.index[j].date()), str(nav.index[i].date()))


def stats(series) -> dict:
    """给一条净值/价格序列算：年化、波动、最大回撤。"""
    import numpy as np

    r = series.pct_change().dropna()
    dd, pk, tr = _max_dd(series)
    return {
        "ann": _ann(r),
        "vol": float(r.std() * np.sqrt(TRADING_DAYS)) if len(r) > 1 else float("nan"),
        "mdd": dd, "peak": pk, "trough": tr, "n": len(series),
    }


# ---------------------------------------------------------------------------

def collect(basket):
    """逐只取数，返回 {code: {...}}。"""
    out = {}
    for code, name, kind in basket:
        st_p, px = _probe(lambda c=code: load_price(c))
        if st_p != "ok" or px is None or len(px) == 0:
            print(f"  [跳过] {code} {name}: 行情不可用（{st_p}）")
            continue
        st_d, dv = _probe(lambda c=code: load_dividend(c))
        if st_d != "ok":
            dv = None
            print(f"  [注意] {code} {name}: 分红不可用（{st_d}）→ 只能给价格口径")
        out[code] = {"name": name, "kind": kind, "px": px, "dv": dv}
        n_div = len(dv) if dv is not None else -1
        print(f"  {code} {name:<12} 行情 {len(px):>5} 行 "
              f"{px['time'].iloc[0].date()}~{px['time'].iloc[-1].date()}  "
              f"分红 {n_div if n_div >= 0 else 'NA'} 条")
    return out


def section_full(data) -> None:
    print()
    print("=" * 116)
    print("第 1 段 全历史：价格口径 vs 全收益口径")
    print("=" * 116)
    print(f"  {'ETF':<14}{'类别':<7}{'区间':<26}{'年数':>5}"
          f"{'价格年化':>10}{'全收益年化':>12}{'分红拖累':>10}{'累计分红':>10}")
    print("  " + "-" * 112)
    for code, d in data.items():
        p, t = total_return_series(d["px"], d["dv"])
        years = (p.index[-1] - p.index[0]).days / 365.25
        sp, stt = stats(p), stats(t)
        cumdiv = float(d["dv"]["cum_div"].iloc[-1]) if d["dv"] is not None and len(d["dv"]) else 0.0
        print(f"  {d['name']:<14}{d['kind']:<7}"
              f"{f'{p.index[0].date()}~{p.index[-1].date()}':<26}{years:>5.1f}"
              f"{sp['ann']:>10.2%}{stt['ann']:>12.2%}"
              f"{(stt['ann'] - sp['ann']) * 100:>9.2f}pp{cumdiv:>10.3f}")


def section_window(data, start: str, end: str) -> None:
    print()
    print("=" * 116)
    print(f"第 2 段 同窗口对比（{start} ~ {end}，与阶段 0 §1 对齐）")
    print("=" * 116)
    print("  为什么要同窗口：不同区间的年化不可比。阶段 0 的仓位标定就发生在这一段。")
    print()
    print(f"  {'ETF':<14}{'类别':<7}{'价格年化':>10}{'全收益年化':>12}{'分红拖累':>10}"
          f"{'价格最大回撤':>14}{'全收益最大回撤':>16}")
    print("  " + "-" * 110)
    import pandas as pd

    for code, d in data.items():
        p, t = total_return_series(d["px"], d["dv"])
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        p2, t2 = p[(p.index >= lo) & (p.index <= hi)], t[(t.index >= lo) & (t.index <= hi)]
        if len(p2) < 250:
            print(f"  {d['name']:<14}{d['kind']:<7}  区间内数据不足（{len(p2)} 行），跳过")
            continue
        sp, stt = stats(p2), stats(t2)
        print(f"  {d['name']:<14}{d['kind']:<7}{sp['ann']:>10.2%}{stt['ann']:>12.2%}"
              f"{(stt['ann'] - sp['ann']) * 100:>9.2f}pp"
              f"{sp['mdd']:>13.2%}{stt['mdd']:>16.2%}")
    print()
    print("  >>> 两个口径的回撤也不同：分红在下跌年份提供缓冲，全收益回撤一般**更小**。")
    print("  >>> 阶段 0 的「权益 ≤25%」是拿价格指数算的 → 用全收益口径**可能放宽**，")
    print("  >>> 但这是阶段 2 的活，本探针只负责把两个数都摆出来。")


def section_coverage(data) -> None:
    print()
    print("=" * 116)
    print("第 3 段 分红数据覆盖率自检（⚠️ 这一段的目的是找出「假 0 分红」）")
    print("=" * 116)
    print("  已知陷阱：红利类 ETF 的分红本就该最多；若某只红利 ETF 报出 0 分红，")
    print("  几乎一定是**数据缺失**而不是事实。")
    print()
    n_zero = 0
    for code, d in data.items():
        dv = d["dv"]
        if dv is None or len(dv) == 0:
            n_zero += 1
            print(f"  {d['name']:<14} 分红记录 **0 条** ← 需人工确认"
                  f"（{d['kind']}类；若为红利类则几乎肯定缺数据）")
        else:
            first = dv["time"].iloc[0].date()
            last = dv["time"].iloc[-1].date()
            print(f"  {d['name']:<14} {len(dv):>3} 条  {first} ~ {last}"
                  f"  累计 {float(dv['cum_div'].iloc[-1]):.3f} 元/份")
    print()
    if n_zero:
        print(f"  >>> {n_zero} 只没有分红记录 → **不能**默认它们不分红，")
        print(f"  >>> 必须在 fetch_etf 落地时对这批标的做人工核对（或换源）。")


def section_conclusion(data) -> None:
    import pandas as pd

    print()
    print("=" * 116)
    print("第 4 段 结论（必须原样抄进文档）")
    print("=" * 116)
    drags = []
    for code, d in data.items():
        p, t = total_return_series(d["px"], d["dv"])
        lo, hi = pd.Timestamp(STAGE0_START), pd.Timestamp(STAGE0_END)
        p2, t2 = p[(p.index >= lo) & (p.index <= hi)], t[(t.index >= lo) & (t.index <= hi)]
        if len(p2) < 250:
            continue
        drags.append((d["name"], stats(t2)["ann"] - stats(p2)["ann"]))
    if drags:
        lo_d = min(x[1] for x in drags)
        hi_d = max(x[1] for x in drags)
        mid = sum(x[1] for x in drags) / len(drags)
        print(f"""
1. 分红拖累（{STAGE0_START}~{STAGE0_END} 同窗口）∈ [{lo_d * 100:.2f}pp, {hi_d * 100:.2f}pp]/年，
   均值 {mid * 100:.2f}pp/年。

2. 对照阶段 0 判据：组合长期年化量级是 3~4%（回撤 ≤10% ⇒ 权益 ≤25%）。
   **分红拖累与它同量级** → 口径选择会直接改写结论，不是"细节"。
   最高的一只是 {max(drags, key=lambda x: x[1])[0]}（{max(x[1] for x in drags) * 100:.2f}pp），
   红利类 ETF 尤其严重：价格序列被除权压住，**收益几乎全在分红里**。

3. 因此三条硬性口径约束：
   - **撮合与估值**：只能用**价格**口径（真实可成交价）；
   - **收益归因与仓位标定**：必须用**全收益**；
   - 产物必须带自描述字段 `price_caliber` ∈
     `raw_no_dividend` / `total_return_dividend_reinvested`，
     **禁止静默混用**。

4. 东财「累计净值」不能当复权因子（含份额折算）；腾讯 `adjust=` 对 ETF
   三档互相矛盾也不可用 → 全收益只能靠「价格 + 新浪逐只分红」自己构造。
""".rstrip())
    else:
        print("  数据不足，无法给结论。")


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 全收益口径对照（只读）")
    ap.add_argument("--codes", nargs="*", default=None,
                    help="只跑指定代码（形如 sh510880），默认跑核心篮子")
    ap.add_argument("--window-start", default=STAGE0_START)
    ap.add_argument("--window-end", default=STAGE0_END)
    args = ap.parse_args()

    basket = CORE_BASKET
    if args.codes:
        want = set(args.codes)
        basket = [b for b in CORE_BASKET if b[0] in want]
        missing = want - {b[0] for b in basket}
        if missing:
            print(f"[注意] 未在核心篮子里登记的代码：{', '.join(sorted(missing))}"
                  f"（名称与类别将留空）")
            basket += [(c, c, "?") for c in sorted(missing)]

    print(f"项目根 = {ROOT}")
    print(f"核心篮子 {len(basket)} 只；取数中（腾讯源单只约 7s，需耐心）…")
    t0 = time.time()
    data = collect(basket)
    if not data:
        print("[错误] 没有取到任何数据")
        return 1
    section_full(data)
    section_window(data, args.window_start, args.window_end)
    section_coverage(data)
    section_conclusion(data)
    print()
    print(f"完成，用时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
