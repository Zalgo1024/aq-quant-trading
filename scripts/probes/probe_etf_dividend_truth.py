"""独立验证：官方「日增长率」是否**含分红**（即是否等于全收益）。

为什么还要验
------------
``probe_etf_dividend_dilution.py`` 已给出结构证据：
- 非除息日（累计比与单位比逐位相等的那天）残差中位 = **0** → 日增长率 == 单位比；
- 除息日残差 ≠ 0 → 日增长率 ≠ 单位比。
两条合起来 ⇒ 日增长率 = 单位比 + 分红/前收 = **全收益**。

但"结构上像"不等于"数值上对"。本探针做**两条完全独立的验证**：

验证 A：逐行证据 —— 把 510880（高分红）全部除息日的
        ``单位比 / 官方日增长率 / 隐含分红``并排打出来，
        Σ隐含分红 / 年数 应落在该 ETF 的历史股息率量级（红利类 3~5%）。

验证 B：与**指数全收益**对照 —— 510880 跟踪上证红利，515080 跟踪中证红利。
        若官方日增长率的年化 ≈ 指数全收益年化，且明显高于单位净值年化，
        则"含分红"这一定性成立（含分红量级也对得上）。

⚠️ 验证 B 只做**方向与量级**的对照：ETF 有费率、跟踪误差、折溢价、
   成分股调整时点差异，不指望逐位吻合。

用法::

    python scripts/probes/probe_etf_dividend_truth.py
"""

from __future__ import annotations

import os
import sys
import time
import warnings
from pathlib import Path

os.environ.setdefault("TQDM_DISABLE", "1")

_here = Path(__file__).resolve()
_root = next(p for p in _here.parents if (p / "config" / "base.yaml").exists())
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

warnings.filterwarnings("ignore")

import akshare as ak  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

pd.set_option("display.width", 240)

#: 逐行证据的标的（分红风格对照）
ROW_CASES = ["510880", "515080", "512890"]
#: 指数全收益对照：(ETF, 名称, [价格指数代码, 全收益指数代码])
INDEX_CASES = [
    ("510880", "上证红利ETF", "000015", "H00015"),
    ("515080", "中证红利ETF", "000922", "H00922"),
]


def load_nav(code: str, tries: int = 4) -> pd.DataFrame:
    last: Exception | None = None
    for k in range(1, tries + 1):
        try:
            raw = ak.fund_etf_fund_info_em(fund=code, start_date="20000101",
                                           end_date="20260915")
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * k)
    else:
        raise RuntimeError(f"{code} 拉净值失败：{type(last).__name__}")
    df = pd.DataFrame({
        "time": pd.to_datetime(raw["净值日期"]),
        "unit_nav": pd.to_numeric(raw["单位净值"], errors="coerce"),
        "cum_nav": pd.to_numeric(raw["累计净值"], errors="coerce"),
        "ret_pct": pd.to_numeric(raw["日增长率"], errors="coerce"),
    }).dropna(subset=["time"])
    df = (df.sort_values("time").drop_duplicates(subset=["time"], keep="last")
          .reset_index(drop=True))
    # ⚠️ 官方日增长率是**百分数**，必须 /100（踩过：当小数用 7/7 事件全错）
    df["ret"] = df["ret_pct"] / 100.0
    return df


def ann(s: pd.Series) -> float:
    s = pd.Series(s).dropna()
    if len(s) < 30:
        return float("nan")
    return float((1.0 + s).prod() ** (250.0 / len(s)) - 1.0)


def years_of(idx: pd.DatetimeIndex) -> float:
    return (idx[-1] - idx[0]).days / 365.25


# ---------------------------------------------------------------------------
# 验证 A：逐行除息日证据
# ---------------------------------------------------------------------------

def row_evidence(code: str) -> None:
    n = load_nav(code).set_index("time")
    u = n["unit_nav"]
    r = n["ret"]
    u_ratio = u / u.shift(1)
    r_ratio = 1.0 + r
    # 隐含每份分红：真实全收益下 d_t = U_{t−1}(1+r_t) − U_t，必须 ≥ 0
    d = (u.shift(1) * r_ratio - u)
    d_pos = d.clip(lower=0.0)

    yrs = years_of(n.index)
    tot_px = float(u.iloc[-1] / u.iloc[0] - 1.0)
    tot_rep = float((1.0 + r).prod() - 1.0)
    d_sum = float(d_pos.sum())
    u_med = float(u.median())
    # 平均年化股息率（粗略：Σd 摊到年数再除中位净值）
    yield_ann = d_sum / yrs / u_med if u_med else float("nan")

    print("=" * 128)
    print(f"验证 A · {code}  区间 {n.index[0].date()} ~ {n.index[-1].date()}"
          f"  共 {yrs:.2f} 年  n={len(n)}")
    print("=" * 128)
    print(f"  单位净值口径 累计收益 = {tot_px * 100:+.1f}%  年化 = {ann(u.pct_change()) * 100:+.2f}%")
    print(f"  官方日增长率 累计收益 = {tot_rep * 100:+.1f}%  年化 = {ann(r) * 100:+.2f}%"
          f"   → 差 {(ann(r) - ann(u.pct_change())) * 100:+.2f}pp/年（= 分红贡献）")
    print(f"  隐含每份分红合计 = {d_sum:.4f} 元 | 单位净值中位 = {u_med:.4f}"
          f" | 摊成年化股息率 ≈ {yield_ann * 100:.2f}%")
    print(f"  隐含分红为负（=模型不成立的证据）的天数 = {int((d < -1e-6).sum())}"
          f"  最小 d = {float(d.min()):.6f}")

    top = d_pos.sort_values(ascending=False).head(12)
    print(f"\n  隐含分红最大的 12 天（同日 单位比 / 官方日增长率 / 差额）：")
    print(f"     {'日期':<12}{'单位净值':>9}{'单位比':>10}{'官方日增长率':>13}"
          f"{'差额(pp)':>11}{'隐含d':>10}{'d/U':>9}")
    for t in top.index:
        i = n.index.get_loc(t)
        if i == 0:
            continue
        print(f"     {t.date()!s:<12}{u.iloc[i]:>9.4f}{u_ratio.iloc[i]:>10.4f}"
              f"{r_ratio.iloc[i]:>13.4f}"
              f"{(r_ratio.iloc[i] - u_ratio.iloc[i]) * 100:>11.3f}"
              f"{d_pos.iloc[i]:>10.4f}{d_pos.iloc[i] / u.iloc[i]:>9.4f}")
    print(f"\n  >>> 判读：若「差额」列在这些天显著 > 0，而其余天为 0，")
    print(f"      则官方日增长率 = 单位净值增长率 + 分红/前收 → **它就是全收益**。")
    print(f"      Σ隐含分红 的年化量级应与该指数历史股息率相符（红利类 3~5%）。")
    print()


# ---------------------------------------------------------------------------
# 验证 B：与指数全收益对照
# ---------------------------------------------------------------------------

def _index_total_ret(symbol: str, start: str, end: str) -> pd.Series | None:
    """取指数日线（优先全收益代码 Hxxxxx，其次普通代码）。"""
    for fn, kw in (
        (ak.stock_zh_index_hist_csindex, {"symbol": symbol}),
        (ak.stock_zh_index_daily_em, {"symbol": symbol}),
    ):
        try:
            d = fn(start_date=start, end_date=end, **kw) if fn is ak.stock_zh_index_hist_csindex \
                else fn(symbol=symbol)
            if d is None or len(d) == 0:
                continue
            cols = {c: c for c in d.columns}
            dcol = next(c for c in d.columns if "日期" in c or c.lower() == "date")
            ccol = next(c for c in d.columns if "收盘" in c or c.lower() == "close")
            s = pd.Series(pd.to_numeric(d[ccol], errors="coerce").values,
                          index=pd.to_datetime(d[dcol])).dropna().sort_index()
            s = s[(s.index >= start) & (s.index <= end)]
            if len(s) > 100:
                return s
        except Exception:  # noqa: BLE001
            continue
    return None


def index_evidence(code: str, name: str, px_idx: str, tr_idx: str) -> None:
    try:
        n = load_nav(code).set_index("time")
    except Exception as exc:  # noqa: BLE001
        print(f"验证 B · {code} 净值失败：{exc}")
        return
    start, end = n.index[0], n.index[-1]
    r = n["ret"]
    yrs = years_of(n.index)

    print("=" * 128)
    print(f"验证 B · {code} {name}  净值区间 {start.date()} ~ {end.date()}（{yrs:.2f} 年）")
    print("=" * 128)
    print(f"  {'口径':<28}{'年化':>10}{'累计倍数':>12}")
    print("  " + "-" * 48)
    for label, s in (("ETF 单位净值", n["unit_nav"].pct_change()),
                     ("ETF 官方日增长率（含分红）", r),
                     ("ETF 累计净值比率", n["cum_nav"].pct_change())):
        tot = float((1.0 + pd.Series(s).dropna()).prod() - 1.0)
        print(f"  {label:<28}{ann(s) * 100:>9.2f}%{1 + tot:>12.4f}")

    for label, sym in (("价格指数", px_idx), ("**全收益指数**", tr_idx)):
        s = _index_total_ret(sym, start, end)
        if s is None or len(s) < 100:
            print(f"  {label:<28}{sym} 不可得")
            continue
        tot = float(s.iloc[-1] / s.iloc[0] - 1.0)
        print(f"  {label:<28}{ann(s.pct_change()) * 100:>9.2f}%{1 + tot:>12.4f}   [{sym}]")
    print()
    print("  >>> 判读：官方日增长率的年化应**落在价格指数与全收益指数之间偏全收益**那一侧。")
    print("      若它反而贴近价格指数、远低于全收益指数，则「含分红」不成立。")
    print()


if __name__ == "__main__":
    for c in ROW_CASES:
        row_evidence(c)
    for code, name, p, t in INDEX_CASES:
        index_evidence(code, name, p, t)
