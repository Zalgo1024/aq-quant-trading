"""ETF 全收益**口径年报**：三口径年化对照 + 逐年归因 + 隐含股息率。

口径裁决（谁才是全收益）
------------------------
本探针是**产出数字**的那个；口径的**证据**在另外两个探针里：

- ``probe_etf_dividend_dilution.py``：结构证据 —— 非除息日残差中位 = 0，
  除息日残差 ≠ 0 ⇒ 官方日增长率 = 单位净值增长率 + 分红/前收。
- ``probe_etf_dividend_truth.py``：逐行证据 —— 510880 的 12 次大额分红全在
  每年 1 月中下旬（上证红利的年度分红时点），同日单位净值跌 ~5% 而官方
  日增长率 ≈ 0%。

结论（三个候选口径）：

| 口径 | 含义 | 能否当全收益 |
|---|---|---|
| **官方「日增长率」** | 单位净值增长率 + 分红/前收，折算日给真实涨跌 | ⭐ **能** |
| 单位净值比率 | 不含分红；且**本身含未记录跳变**（折算/历史重述） | ❌ |
| 累计净值比率 | ``累计 = 单位 + 累计分红 D`` ⇒ ``累计比 = r/(1+D/U)``，**被稀释** | ❌ |

⚠️ 累计比看起来"更官方"其实是陷阱：不分红的 512890 上它与官方日增长率
偏差**恰好为 0**，分红标的 510880 上 56.8% 交易日偏差 >5e-4 ——
偏差只来自分红，正是稀释特征。本项目一度误採累计比，已纠正。

⚠️ 已知的方法学缺口（如实记录，未解决）
- **指数全收益对照做不了**：``stock_zh_index_hist_csindex`` 取不到
  H00015 / H00922（实测"不可得"）→ 缺一条完全外部的锚。
- 「全收益 − 单位净值」的年化差**不等于**股息率：被单位净值自身的
  未记录跳变污染（510880 实测 0.95pp/年 vs 隐含 Σd 摊 3.19%）。
  所以两列都只当量级参考，不要拿去做精确归因。

用法::

    python scripts/probes/probe_etf_nav_caliber.py
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

FOCUS = [
    "512890",  # 红利低波 —— B 路线核心
    "510880",  # 红利ETF（年度 1 月分红）
    "515180", "563020", "515080",   # 其他红利
    "510300", "510500", "159915", "588000",   # 宽基
    "513100", "513500", "159920",   # 跨境
    "511010", "511880",             # 债
    "518880",                       # 商品
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
        # ⚠️ 官方日增长率是**百分数**，必须 /100
        "ret": pd.to_numeric(raw["日增长率"], errors="coerce") / 100.0,
    }).dropna(subset=["time"])
    return (df.sort_values("time").drop_duplicates(subset=["time"], keep="last")
            .set_index("time").reset_index())


def ann(s: pd.Series) -> float:
    s = pd.Series(s).dropna()
    if len(s) < 30:
        return float("nan")
    return float((1.0 + s).prod() ** (250.0 / len(s)) - 1.0)


def sharpe(s: pd.Series) -> float:
    s = pd.Series(s).dropna()
    if len(s) < 30 or s.std(ddof=1) == 0:
        return float("nan")
    return float(s.mean() / s.std(ddof=1) * np.sqrt(250.0))


def mdd(s: pd.Series) -> float:
    nav = (1.0 + pd.Series(s).dropna()).cumprod()
    return float((nav / nav.cummax() - 1.0).min())


def price_ann(code: str, start: pd.Timestamp, end: pd.Timestamp) -> float:
    pfx = "sz" if code.startswith(("15", "16")) else "sh"
    p = ak.stock_zh_a_hist_tx(symbol=pfx + code, start_date="20040101",
                              end_date="20260915")
    p["date"] = pd.to_datetime(p["date"])
    p["close"] = pd.to_numeric(p["close"], errors="coerce")
    s = (p.dropna(subset=["date"]).sort_values("date")
         .drop_duplicates(subset=["date"], keep="last").set_index("date")["close"])
    s = s[(s.index >= start) & (s.index <= end)]
    return ann(s.pct_change()), s


def main() -> int:
    t0 = time.time()
    print("=" * 132)
    print("ETF 全收益口径年报")
    print("  全收益 = 官方「日增长率」累乘（含分红、折算中性）")
    print("  单位比 = 单位净值比率（不含分红，且含未记录跳变）")
    print("  累计比 = 累计净值比率（= r/(1+D/U)，被累计分红稀释）")
    print("=" * 132)
    sys.stdout.flush()

    rows = []
    for code in FOCUS:
        try:
            n = load_nav(code).set_index("time")
        except Exception as exc:  # noqa: BLE001
            print(f"  {code} 失败：{exc}")
            continue
        try:
            p_ann, _ = price_ann(code, n.index[0], n.index[-1])
        except Exception:  # noqa: BLE001
            p_ann = float("nan")
        r = n["ret"]
        u = n["unit_nav"]
        yrs = (n.index[-1] - n.index[0]).days / 365.25
        # 隐含每份分红（由「官方日增长率 = 全收益」反解；clip ≥0 后再合计）
        d = (u.shift(1) * (1.0 + r) - u).clip(lower=0.0)
        dy = float(d.sum() / yrs / u.median()) if u.median() else float("nan")
        # ⭐ 入库不变量（与 ``EtfNavStore.verify`` 同一判据，别再另立一份）：
        #   d_t/U_{t−1} = r_t − u_t ⇒ |r−u| 就是"每份分红占前收的比例"，
        #   中位数对 1~12 天/年的分红日稳健；口径错 100 倍会让全体日期一起偏。
        # ❌ 不要用「只在 |累计比 − 单位比| < 1e-9 的日子比较」那个掩码 ——
        #   两个比率都由 4 位小数净值算出，逐位相等基本只在"净值完全没动、
        #   双双为 0"时成立 → 退化样本，对 510300/511010 全部为 0、灵敏度归零。
        # ❌ 也不要用 max —— 会被净值归一日（511880 的 99.07）与源数据单日孤点占满。
        dev = (r - u.pct_change()).dropna().abs()
        dev_med = float(dev.median()) if len(dev) else float("nan")
        dev_max = float(dev.max()) if len(dev) else float("nan")
        n_div = int((dev > 1e-3).sum())
        rows.append({
            "code": code,
            "区间": f"{n.index[0].date()}~{n.index[-1].date()}",
            "年数": yrs,
            "原始价格": p_ann,
            "单位比": ann(u.pct_change()),
            "累计比": ann(n["cum_nav"].pct_change()),
            "**全收益**": ann(r),
            "夏普": sharpe(r),
            "最大回撤": mdd(r),
            "隐含股息率": dy,
            "不变量中位": dev_med,
            "不变量max": dev_max,
            "分红日": n_div,
            "比对数": int(len(dev)),
        })
    df = pd.DataFrame(rows)

    print()
    fmt = {c: "{:.2%}".format for c in
           ("原始价格", "单位比", "累计比", "**全收益**", "最大回撤", "隐含股息率")}
    fmt.update({"年数": "{:.2f}".format,
                "夏普": "{:.3f}".format,
                "不变量中位": "{:.2e}".format,
                "不变量max": "{:.2e}".format})
    print(df[["code", "年数", "原始价格", "单位比", "累计比", "**全收益**",
              "夏普", "最大回撤", "隐含股息率"]].to_string(index=False, formatters=fmt))
    print()
    print("  ⚠️ 「原始价格」列对发生过份额折算的标的（512890/510500/159915 等）")
    print("     严重失真 —— 它把 1:2 拆分当成了 −51% 的亏损。逐只原因见")
    print("     ``probe_etf_split.py``。")
    print()
    print("  入库不变量：``median|nav_ret − 单位比|`` 应 ≈ 源数据舍入步长")
    print("     （官方日增长率只保留 2 位小数百分比 ⇒ 前收 0.01%/2 = 5e-5）：")
    inv = df[["code", "比对数", "不变量中位", "不变量max", "分红日"]]
    print(inv.to_string(index=False, formatters={
        "不变量中位": "{:.2e}".format, "不变量max": "{:.2e}".format}))
    bad = df[df["不变量中位"] > 1e-4]
    print(f"     中位超 1e-4 的标的 = {len(bad)} 只 "
          f"{list(bad['code']) if len(bad) else '（无）'}")
    print("     「分红日」= ``|r − 单位比| > 0.1%`` 的天数；它应显著小于总天数")
    print("     （分红一年才几天），且**不分红标的应≈0** —— 这是判据自身的交叉验证。")
    print("     「不变量max」只作参考：会被净值归一日与源数据单日孤点占满。")
    print()
    print("=" * 132)
    print("512890 红利低波ETF 逐年归因（B 路线核心标的）")
    print("=" * 132)
    try:
        n = load_nav("512890").set_index("time")
        _, p = price_ann("512890", n.index[0], n.index[-1])
        d = pd.DataFrame({"全收益": n["ret"]}).join(
            pd.DataFrame({"原始价格": p.pct_change()}), how="outer").sort_index()
        d = d[d.index >= "2019-01-01"]
        g = d.groupby(d.index.year).apply(
            lambda x: pd.Series({
                "全收益%": ((1 + x["全收益"].dropna()).prod() - 1) * 100,
                "原始价格%": ((1 + x["原始价格"].dropna()).prod() - 1) * 100,
                "天数": int(x["全收益"].notna().sum()),
            }), include_groups=False)
        print(g.round(2).to_string())
        full = n[n.index >= "2019-01-18"]
        tot = float((1.0 + full["ret"]).prod() - 1.0)
        yrs = len(full) / 250.0
        print()
        print(f"  2019-01-18 ~ {full.index[-1].date()}  共 {yrs:.2f} 年")
        print(f"  全收益累计 = {tot * 100:+.1f}%  → 年化 {((1 + tot) ** (1 / yrs) - 1) * 100:+.2f}%")
        print(f"  夏普 = {sharpe(full['ret']):.3f}   最大回撤 = {mdd(full['ret']):.2%}")
    except Exception as exc:  # noqa: BLE001
        print(f"  512890 逐年归因失败 {type(exc).__name__}: {exc}")
    print()
    print("完成，用时 %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
