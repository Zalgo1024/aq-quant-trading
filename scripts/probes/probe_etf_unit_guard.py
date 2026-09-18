"""诊断：单位守卫在 510050 / 511880 上是否判错（含货币 ETF 这种日涨幅极小的情形）。

背景
----
`coerce_return_unit` 用「99 分位 |r| > 0.30 ⇒ 百分数」判定。对**货币 ETF**
（511880，日涨幅 ~0.005）失效：只要有个别巨额脏值把 99 分位顶上去，
整列就被误判成百分数、被多除 100 倍 → 口径校验的偏离正好是 99.0（=100−1）。

本探针把判定改为**以单位净值为锚**：在「累计比 == 单位比」的那些天（当日无
分红计入）上，比较 `|单位比 − r|` 与 `|单位比 − r/100|`，谁小取谁。
锚是净值表自己提供的，因此判定与后续不变量天然自洽。

用法::

    python scripts/probes/probe_etf_unit_guard.py
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

pd.set_option("display.width", 220)

#: 诊断样本：货币 / 债券 / 宽基 / 分红 / 跨境 各取一只
CASES = [
    ("511880", "银华日利（货币ETF，日涨幅极小）"),
    ("511010", "国债ETF"),
    ("511260", "十年国债ETF"),
    ("510050", "上证50ETF（年度分红）"),
    ("510300", "沪深300ETF"),
    ("512890", "红利低波ETF（不分红）"),
    ("513100", "纳指ETF（有折算）"),
]


def load_raw(code: str, tries: int = 4) -> pd.DataFrame:
    """**原样**保留日增长率列（不做任何单位假设），同时取单位/累计净值。"""
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
        "raw_ret": pd.to_numeric(raw["日增长率"], errors="coerce"),  # 原样
    }).dropna(subset=["time"])
    return (df.sort_values("time").drop_duplicates(subset=["time"], keep="last")
            .set_index("time").reset_index())


def diagnose(code: str, note: str) -> dict | None:
    try:
        n = load_raw(code).set_index("time")
    except Exception as exc:  # noqa: BLE001
        print(f"  {code} 失败：{exc}")
        return None
    u = n["unit_nav"]
    u_ratio = u.pct_change()
    c_ratio = n["cum_nav"].pct_change()
    raw = n["raw_ret"].dropna()

    # 锚点：累计比与单位比逐位相等的那些天（当日无分红计入）
    nd = (c_ratio - u_ratio).abs() < 1e-9
    anchor = u_ratio[nd].dropna()
    raw_nd = raw.reindex(anchor.index).dropna()
    anchor = anchor.reindex(raw_nd.index)

    # 三种假设的拟合误差（中位绝对偏差）
    e_as_is = (anchor - raw_nd).abs().median()
    e_div100 = (anchor - raw_nd / 100.0).abs().median()
    e_mul100 = (anchor - raw_nd * 100.0).abs().median()

    q99 = float(raw.abs().quantile(0.99))
    med = float(raw.abs().median())
    # 生产级联的两路判据分别是什么（用同一份数据，不重算网络）
    from aq.data.etf_actions import coerce_return_unit

    _fx_anchor, unit_anchor = coerce_return_unit(n["raw_ret"], anchor=u_ratio)
    _fx_none, unit_none = coerce_return_unit(n["raw_ret"], anchor=None)
    return {
        "code": code,
        "标的": note,
        "n": len(n),
        "原始 中位|r|": med,
        "原始 99分位|r|": q99,
        "锚点数": int(len(anchor)),
        "err 原样": e_as_is,
        "err ÷100": e_div100,
        "err ×100": e_mul100,
        "**锚判定**": "原样" if e_as_is <= min(e_div100, e_mul100)
                       else ("÷100" if e_div100 <= e_mul100 else "×100"),
        "生产①锚": unit_anchor,
        "生产③兜底": unit_none,
    }


if __name__ == "__main__":
    print("=" * 128)
    print("单位守卫诊断：三级级联（①锚 → ②量级闸门 → ③99分位兜底）")
    print("  锚点 = 「累计比与单位比逐位相等」的天数（当日无分红计入），")
    print("  这些天上官方日增长率必须等于单位净值增长率 → 可直接判单位。")
    print("  ⚠️ 但**债基/货币 ETF 的锚点日常常单位净值一动不动**，三档误差全 0")
    print("     → 锚没有信息量，必须交给「②量级闸门」（年化是否合理）。")
    print("=" * 128)
    rows = [r for r in (diagnose(c, t) for c, t in CASES) if r]
    df = pd.DataFrame(rows)
    print()
    print(df.to_string(index=False, formatters={
        "原始 中位|r|": "{:.6f}".format, "原始 99分位|r|": "{:.4f}".format,
        "err 原样": "{:.2e}".format, "err ÷100": "{:.2e}".format,
        "err ×100": "{:.2e}".format}))
    print()
    want = "÷100"
    ok_anchor = df[df["生产①锚"] == df["生产③兜底"]]
    print(f"生产级联判定（全部应为 ÷100，因为实测 18 只 core 的日增长率都是百分数）：")
    bad = df[df["生产①锚"] != want]
    print(f"  ①锚一路判错/未定的 = {len(bad)} 只 {list(bad['code']) if len(bad) else '（无）'}")
    bad3 = df[df["生产③兜底"] != want]
    print(f"  ③只用兜底的误判     = {len(bad3)} 只 {list(bad3['code']) if len(bad3) else '（无）'}")
    tie = df[(df["err 原样"] == 0) & (df["err ÷100"] == 0)]
    print(f"  锚点退化为平局的     = {len(tie)} 只 {list(tie['code']) if len(tie) else '（无）'}"
          f"  ← 这些必须由 ②量级闸门 救回")
    print()
    print("  >>> 判读：①锚 应修好货币 ETF（中位|r|=0.01，落进灰色带），")
    print("      ②量级闸门 应修好债基/宽基的锚点平局。两路都不可省。")
