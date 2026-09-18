# -*- coding: utf-8 -*-
"""阶段 B 的零假设基线：直接买「红利 / 低波」ETF 到底有多强？

研究背景
--------
`docs/终局诊断与重启方案.md` 把路线定为
阶段 A 无偏数据底座 → **阶段 B 红利低波（零假设＝直接买红利 ETF）** → 阶段 C ETF 轮动。
本探针回答的是 B 的**零假设本身有多大**：如果"躺平买红利 ETF"已经很强，
那 B 阶段任何策略都必须跑赢它才算有价值（与 A 阶段"跑不赢直接买 ETF 就别做"同一把尺子）。

口径（改动本文件前必读）
------------------------
1. **收益唯一权威 = `EtfNavStore.total_return`**（东财官方「日增长率」，已 /100 存小数）。
   绝不用累计净值比率（被分红稀释）、也不用单位净值比率（不含分红）。
2. ⚠️ **绝对年化不可引用**：`etf_list` 是**当前**快照，只含尚未清盘的 ETF，
   存在幸存者偏差（已在 docs §8 量化为上界）。因此本探针**只报"相对同池"的超额**，
   绝对数字仅供核对、不得写进结论。
3. **比较池 = 境内权益 ETF**（按名称剔除债券 / 货币 / 商品 / 跨境）。
   ⚠️ 为什么必须先剔：池里塞进货基、债基、黄金后，"同池等权"会被年化 2% 的低波资产
   拉低，红利组相对它必然是巨额正超额 —— 那是**基准被稀释**的假象，不是红利有 alpha。
4. 两个基准同时报：
   - **全权益池等权** = 池内所有境内权益 ETF 的日收益算术平均（每日再平衡）；
   - **宽基等权**     = 名称命中沪深300/中证500/上证50/创业板/科创50/中证1000/A500 的 ETF 等权，
     这是"什么都不做、就买大盘"的代理，也是最苛刻、最该被跑赢的尺子。
5. 无风险利率 rf = 2%/年，按 252 交易日折算到日。
6. **对齐口径（2026-09-18 修正，见下方"对齐 bug"注释）**：
   先按窗口做**覆盖预筛**，再取交集 `[max(first), min(last)]`。

对齐 bug（2026-09-18 实踩，别再犯）
-----------------------------------
初版先建面板、后筛样本量：起点取 ``firsts.max()``（样本中最晚的起始日）。
池里只要有一只 2026-09-15 才上市的新 ETF，对齐起点就被顶到 2026-09-15，
面板塌成 **1 个交易日**，随后所有"至少 5 年"的过滤把样本清零 →
输出 `进入统计: 0 只 / 1 个交易日` 并判"样本太少"。
修法：**预筛必须发生在对齐之前** —— 先要求每只 ETF 的 first ≤ 窗口起点+slack、
last ≥ 窗口终点−slack，再在幸存样本上取交集（起点取 max、终点取 **min**）。

用法
----
    python scripts/probes/probe_dividend_etf_baseline.py
    python scripts/probes/probe_dividend_etf_baseline.py --start 2019-01-01 --min-years 5
    python scripts/probes/probe_dividend_etf_baseline.py --start 2021-01-01 --min-years 3

只读：不写任何数据产物、不改任何落盘文件。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 路径解析：向上找 config/base.yaml（项目约定，别写 parents[1]）
HERE = Path(__file__).resolve()
ROOT = HERE
while ROOT and not (ROOT / "config" / "base.yaml").exists():
    if ROOT.parent == ROOT:
        break
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from aq.data.etf_store import EtfNavStore, load_etf_list  # noqa: E402

TRADING_DAYS = 252
RF_ANNUAL = 0.02

# ---------------------------------------------------------------- 资产类别剔除
# 目的：把比较池收敛到「境内权益」，否则基准被债/货/商品稀释。
# 宁可多剔，不可少剔 —— 少剔一只债基就是往基准里掺水。
EXCLUDE_ASSET: dict[str, tuple[str, ...]] = {
    "债券": ("债", "国债", "政金", "城投", "短融", "信用", "转债", "利率债", "地方债"),
    "货币": ("货币", "现金", "日利", "添益", "添利", "保证金", "理财", "宝利", "快线"),
    "商品": ("黄金", "豆粕", "有色", "能源", "原油", "商品", "白银", "铜", "铁矿石"),
    "跨境": ("纳指", "纳斯达克", "标普", "道琼斯", "恒生", "恒指", "港股", "H股",
             "日经", "德国", "法国", "日本", "中概", "海外", "东南亚", "沙特",
             "美国", "越南", "印度", "亚太", "全球", "QDII"),
}

# ---------------------------------------------------------------- 研究分组（境内权益内部）
GROUP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "红利": ("红利", "股息", "高股息"),
    "低波": ("低波", "低波动"),
    "宽基": ("沪深300", "沪深 300", "中证500", "中证 500", "中证800", "中证 800",
             "上证50", "上证 50", "创业板", "科创50", "科创 50", "科创100",
             "中证1000", "中证 1000", "中证2000", "A500", "A50", "MSCI"),
}
# 宽基补充：纯数字代码的简称（名称里没有中文指数名时兜底）
BROAD_EXTRA = ("300ETF", "500ETF", "50ETF", "1000ETF", "800ETF", "A500")

GROUPS = ("红利", "低波", "红利+低波", "宽基")


def asset_class(name: str) -> str | None:
    """返回该 ETF 所属的**非权益**类别（债/货币/商品/跨境）；None = 疑似境内权益。"""
    for cls, kws in EXCLUDE_ASSET.items():
        if any(k in name for k in kws):
            return cls
    return None


def classify(name: str) -> str | None:
    """在境内权益内部给 ETF 分组；返回 None 表示不进任何研究组（仍留在比较池里）。"""
    hits = [g for g, kws in GROUP_KEYWORDS.items() if any(k in name for k in kws)]
    if not hits and any(k in name for k in BROAD_EXTRA):
        hits = ["宽基"]
    if not hits:
        return None
    if "红利" in hits and "低波" in hits:
        return "红利+低波"
    if "红利" in hits:
        return "红利"
    if "低波" in hits:
        return "低波"
    return "宽基" if "宽基" in hits else None


def metrics(r: pd.Series, rf_daily: float) -> dict:
    """给一段日收益序列算指标。`r` 必须是**小数**收益。"""
    r = r.dropna()
    n = len(r)
    if n < 60:
        return {}
    growth = float((1.0 + r).prod())
    years = n / TRADING_DAYS
    cagr = growth ** (1.0 / years) - 1.0
    mu_d = float(r.mean())
    sd_d = float(r.std(ddof=1))
    vol = sd_d * np.sqrt(TRADING_DAYS)
    sharpe = (mu_d - rf_daily) / sd_d * np.sqrt(TRADING_DAYS) if sd_d > 0 else float("nan")
    curve = (1.0 + r).cumprod()
    mdd = float((curve / curve.cummax() - 1.0).min())
    # 均值显著性的朴素 t（未做自相关调整；日收益自相关很弱，够用作量级判读）
    t_mean = mu_d / (sd_d / np.sqrt(n)) if sd_d > 0 else float("nan")
    return {
        "n_days": n,
        "years": years,
        "total": growth - 1.0,
        "cagr": cagr,
        "vol": vol,
        "sharpe": sharpe,
        "max_dd": mdd,
        "calmar": (cagr / abs(mdd)) if mdd < 0 else float("nan"),
        "t_mean": t_mean,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--min-years", type=float, default=5.0,
                    help="对齐后的区间至少要这么长，否则判样本不足")
    ap.add_argument("--slack-days", type=int, default=20,
                    help="覆盖预筛的容差（自然日）：允许上市/停更比窗口边界晚/早这么多天")
    ap.add_argument("--max-nan", type=float, default=0.02,
                    help="对齐区间内允许的缺失比例上限")
    args = ap.parse_args()

    nav = EtfNavStore()
    lst = load_etf_list()
    print("=" * 82)
    print("阶段 B 零假设基线：红利 / 低波 ETF 相对「境内权益池」能跑出多少超额")
    print("=" * 82)
    print(f"ETF 清单      : {len(lst)} 只")
    print(f"有净值落盘    : {len(nav.available())} 只")
    print(f"窗口          : {args.start} ~ {args.end or '数据最新日'}")
    print(f"预筛容差      : ±{args.slack_days} 自然日")

    # ---- 1) 读全收益序列（权威口径：官方日增长率）
    rf_daily = RF_ANNUAL / TRADING_DAYS
    rows: list[tuple[str, pd.Series]] = []
    for code in nav.available():
        try:
            s = nav.total_return(code, start=args.start, end=args.end)
        except Exception:
            continue
        s = s.dropna()
        if s.empty:
            continue
        rows.append((code, s))
    print(f"窗口内有净值  : {len(rows)} 只")

    if not rows:
        print("!! 窗口内没有任何 ETF 净值，先跑 scripts/fetch_etf.py --all --nav 补数据")
        return 1

    # ---- 2) 资产类别剔除：只留境内权益
    name_of = dict(zip(lst["symbol"].astype(str), lst["name"].astype(str)))
    for c, _ in rows:
        name_of.setdefault(c, name_of.get(c.split(".")[0], c))
    excluded: dict[str, list[str]] = {}
    kept_rows = []
    for c, s in rows:
        nm = name_of.get(c, "")
        cls = asset_class(nm)
        if cls:
            excluded.setdefault(cls, []).append(c)
        else:
            kept_rows.append((c, s))
    exc_desc = "、".join(f"{k} {len(v)}" for k, v in sorted(excluded.items()))
    print(f"剔除非权益    : {len(rows) - len(kept_rows)} 只（{exc_desc}）")
    print(f"境内权益候选  : {len(kept_rows)} 只")

    if len(kept_rows) < 10:
        print("!! 权益池太小，结论不可引用")
        return 1

    # ---- 3) 覆盖预筛（**必须在对齐之前**，见模块 docstring 的"对齐 bug"）
    firsts_all = pd.Series({c: s.index.min() for c, s in kept_rows})
    lasts_all = pd.Series({c: s.index.max() for c, s in kept_rows})
    data_asof = lasts_all.max()
    win_start = pd.Timestamp(args.start)
    win_end = pd.Timestamp(args.end) if args.end else data_asof
    slack = pd.Timedelta(days=args.slack_days)

    print("\n起始日分布（境内权益候选，按年）:")
    by_year = firsts_all.dt.year.value_counts().sort_index()
    for y, n in by_year.items():
        print(f"  {int(y)} 年首批净值: {int(n):>5} 只")
    print(f"数据最新日    : {pd.Timestamp(data_asof).date()}")

    ok = [c for c, s in kept_rows
          if firsts_all[c] <= win_start + slack and lasts_all[c] >= win_end - slack]
    print(f"\n覆盖预筛后    : {len(ok)} 只（first ≤ {win_start.date()}+{args.slack_days}d "
          f"且 last ≥ {win_end.date()}-{args.slack_days}d）")

    if len(ok) < 10:
        print("!! 覆盖窗口的样本太少。要么放宽 --start（往后推），要么放宽 --slack-days。")
        print(f"   提示：数据里最晚的首批净值是 {pd.Timestamp(firsts_all.max()).date()}，"
              f"若它 > 窗口起点，说明有很新的 ETF 把窗口顶住了。")
        return 1

    # ---- 4) 对齐：预筛后的样本取交集（起点 max、终点 min）
    sub = {c: s for c, s in kept_rows if c in set(ok)}
    start_aligned = max(s.index.min() for s in sub.values())
    end_aligned = min(s.index.max() for s in sub.values())
    span_years = (end_aligned - start_aligned).days / 365.25
    print(f"对齐区间      : {pd.Timestamp(start_aligned).date()} ~ "
          f"{pd.Timestamp(end_aligned).date()}（{span_years:.2f} 年）")

    if span_years < args.min_years:
        print(f"!! 对齐后区间仅 {span_years:.2f} 年 < --min-years {args.min_years}，结论不可引用")
        print("   放宽办法：--min-years 调小 / --start 往后推 / --slack-days 调大")
        return 1

    # ---- 4b) 先定**交易日历**，再筛样本
    # ⚠️ 为什么必须先定日历（2026-09-18 实踩）：直接 `pd.DataFrame({c: s})` 会取**并集**索引，
    # 而部分 ETF 的净值表含非交易日行（货基/债基按自然日披露的残留），
    # 把并集撑到 2791 天（7.64 年真实交易日只有 ~1856 天）。
    # 于是"缺失 ≤2%"这个阈值对正常权益 ETF（只覆盖交易日）恒不成立 → 123/132 只被误杀，只剩 9 只。
    # 修法：只保留**被多数 ETF 共同覆盖**的日期当交易日历，再在日历上算缺失率。
    raw = pd.DataFrame({c: s.loc[start_aligned:end_aligned] for c, s in sub.items()}).sort_index()
    counts = raw.notna().sum(axis=1)
    thr_cal = max(3, int(0.5 * raw.shape[1]))
    cal = counts >= thr_cal
    panel = raw.loc[cal]
    expect = int(panel.shape[0])
    med_obs = int(counts[cal].median())
    print(f"交易日历      : {expect} 天（并集 {len(raw)} 天 → 剔除非交易日 {len(raw) - expect} 天；"
          f"口径 = 当日至少 {thr_cal} 只有净值）")
    print(f"日历上中位覆盖: {med_obs} 只/天")

    min_obs = int(len(panel) * (1.0 - args.max_nan))
    before = panel.shape[1]
    panel = panel.loc[:, panel.notna().sum() >= min_obs]
    print(f"进入统计      : {panel.shape[1]} 只 / {panel.shape[0]} 个交易日 "
          f"（缺失 ≤ {args.max_nan:.0%}，剔除 {before - panel.shape[1]} 只缺数据的）")

    if panel.shape[1] < 10:
        print("!! 样本太少，结论不可引用")
        return 1

    # ---- 4c) 数据缺陷剔除：收益恒为 0 / 常数的标的
    # ⚠️ 实踩：511850 / 511960 / 511970 三只净值列全空 → 日收益恒 0，
    # 混进红利组后把整组年化从 ~11% 砸到 2.83%，且"波动 0"会伪造成"完美避险资产"。
    sd_all = panel.std(ddof=1)
    bad = sorted(sd_all[(sd_all.isna()) | (sd_all < 1e-8)].index.tolist())
    if bad:
        print(f"数据缺陷剔除  : {len(bad)} 只（日收益恒为 0，净值列疑似全空）:")
        for c in bad[:20]:
            print(f"    - {c}  {name_of.get(c, '')}")
        panel = panel.drop(columns=bad)

    # ---- 5) 分组
    groups: dict[str, list[str]] = {}
    ungrouped = 0
    for c in panel.columns:
        g = classify(name_of.get(c, ""))
        if g:
            groups.setdefault(g, []).append(c)
        else:
            ungrouped += 1

    print("\n分组（按名称关键词，只分得清的才进组；未分组的仍留在比较池里）:")
    for g, cs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"  {g:8} {len(cs):>4} 只")
    print(f"  {'未分组':8} {ungrouped:>4} 只（主题/行业 ETF 等）")
    # 成员审计：分类靠关键词，必须能肉眼看出来分错没有
    for g in ("红利", "低波", "红利+低波"):
        cs = groups.get(g, [])
        if cs:
            print(f"  [{g}] " + "、".join(f"{c} {name_of.get(c, '')}" for c in cs))

    # ---- 6) 两个基准
    eq_all = panel.mean(axis=1)                       # 境内权益池等权
    broad = groups.get("宽基", [])
    eq_broad = panel[broad].mean(axis=1) if len(broad) >= 3 else None
    if eq_broad is None:
        print("!! 宽基样本 < 3 只，宽基基准不报")

    # 基准 C：沪深300 ETF 等权 —— 最"标准"的买大盘。
    # 为什么单列：宽基等权里混了创业板/科创50/中证1000/2000，2019–2021 成长风格强势，
    # 拿它当基准会系统性压低红利的读数；沪深300 才是不带风格倾斜的那把尺子。
    hs = [c for c in panel.columns
          if ("沪深300" in name_of.get(c, "") or "沪深 300" in name_of.get(c, ""))]
    eq_hs = panel[hs].mean(axis=1) if len(hs) >= 3 else None
    if eq_hs is None:
        print(f"!! 沪深300 ETF 仅 {len(hs)} 只（<3），基准 C 不报")
    m_all = metrics(eq_all, rf_daily)
    m_broad = metrics(eq_broad, rf_daily) if eq_broad is not None else {}
    m_hs = metrics(eq_hs, rf_daily) if eq_hs is not None else {}

    # t 列统一口径：报「超额年化的 t」= 日超额均值 t / √(年数)
    def ann_t(ex: pd.Series) -> float:
        sd = float(ex.std(ddof=1))
        if sd <= 0:
            return float("nan")
        t_daily = float(ex.mean()) / (sd / np.sqrt(len(ex)))
        return t_daily / np.sqrt(len(ex) / TRADING_DAYS)

    def block2(title: str, bench: pd.Series, bm: dict, bench_n: int) -> list[dict]:
        print("\n" + "=" * 82)
        print(f"【{title}】各组等权组合 vs 基准")
        print("⚠️ 绝对年化含幸存者偏差（现役快照），不可引用 → 只看超额列；"
              "t(超额) = 超额夏普 ÷ √年数 的量级判读")
        print("=" * 82)
        hdr = (f"{'组':<10}{'只数':>5}{'年化':>9}{'波动':>8}{'夏普':>8}{'最大回撤':>10}"
               f"{'超额年化':>10}{'超额波动':>10}{'超额夏普':>10}{'t(超额)':>9}")
        print(hdr)
        print("-" * len(hdr))
        out = []
        for g in GROUPS:
            cs = groups.get(g)
            if not cs:
                continue
            port = panel[cs].mean(axis=1)
            m = metrics(port, rf_daily)
            ex = (port - bench).dropna()
            sd_ex = float(ex.std(ddof=1)) * np.sqrt(TRADING_DAYS)
            mu_ex_ann = float(ex.mean()) * TRADING_DAYS
            sharpe_ex = mu_ex_ann / sd_ex if sd_ex > 0 else float("nan")
            t_ex = ann_t(ex)
            exc_cagr = m["cagr"] - bm["cagr"]
            print(f"{g:<10}{len(cs):>5}{m['cagr']:>9.2%}{m['vol']:>8.2%}{m['sharpe']:>8.2f}"
                  f"{m['max_dd']:>10.2%}{exc_cagr:>10.2%}{sd_ex:>10.2%}"
                  f"{sharpe_ex:>10.2f}{t_ex:>9.2f}")
            out.append({"group": g, "n": len(cs), "cagr": m["cagr"], "vol": m["vol"],
                        "sharpe": m["sharpe"], "max_dd": m["max_dd"],
                        "exc_cagr": exc_cagr, "exc_vol": sd_ex,
                        "exc_sharpe": sharpe_ex, "t_ex": t_ex})
        print(f"{'基准':<10}{bench_n:>5}{bm['cagr']:>9.2%}{bm['vol']:>8.2%}{bm['sharpe']:>8.2f}"
              f"{bm['max_dd']:>10.2%}{0.0:>10.2%}{0.0:>10.2%}{0.0:>10.2f}{0.0:>9.2f}")
        return out

    block2("基准 A：境内权益池等权", eq_all, m_all, panel.shape[1])
    if eq_broad is not None:
        block2("基准 B：宽基 ETF 等权（含创业板/科创/中证1000，成长倾斜）",
               eq_broad, m_broad, len(broad))
    if eq_hs is not None:
        block2("基准 C：沪深300 ETF 等权（不带风格倾斜的买大盘）",
               eq_hs, m_hs, len(hs))

    # ---- 7) 明细：红利/低波组里超额最高的 12 只
    print("\n" + "=" * 82)
    b_detail, m_detail = ((eq_hs, m_hs) if eq_hs is not None
                          else ((eq_broad, m_broad) if eq_broad is not None else (eq_all, m_all)))
    print(f"【明细】红利 / 低波 组：按相对「{'沪深300ETF等权' if eq_hs is not None else '宽基等权'}」"
          f"的超额年化排序，前 12 只")
    print("=" * 82)
    bench_detail = b_detail
    det = []
    for g in ("红利", "低波", "红利+低波"):
        for c in groups.get(g, []):
            m = metrics(panel[c], rf_daily)
            if not m:
                continue
            ex = (panel[c] - bench_detail).dropna()
            sd_ex = float(ex.std(ddof=1)) * np.sqrt(TRADING_DAYS)
            det.append({
                "symbol": c, "name": name_of.get(c, "")[:14], "group": g,
                "cagr": m["cagr"], "vol": m["vol"], "sharpe": m["sharpe"],
                "max_dd": m["max_dd"],
                "exc_cagr": m["cagr"] - m_detail["cagr"],
                "exc_sharpe": (float(ex.mean()) * TRADING_DAYS / sd_ex) if sd_ex > 0 else float("nan"),
                "t_ex": ann_t(ex),
            })
    d = pd.DataFrame(det).sort_values("exc_cagr", ascending=False)
    with pd.option_context("display.width", 220, "display.max_columns", 20):
        print(d.head(12).to_string(index=False,
              formatters={"cagr": "{:.2%}".format, "vol": "{:.2%}".format,
                          "max_dd": "{:.2%}".format, "exc_cagr": "{:.2%}".format,
                          "sharpe": "{:.2f}".format, "exc_sharpe": "{:.2f}".format,
                          "t_ex": "{:.2f}".format}))

    # ---- 8) 结论口径
    print("\n" + "-" * 82)
    print("结论口径提醒：")
    print("  1. 收益来源 = 东财官方「日增长率」（含分红、折算中性），非价格涨跌。")
    print("  2. 绝对年化**不可引用**：etf_list 是现役快照，清盘 ETF 不在里面。")
    print("  3. 比较池已剔除债券/货币/商品/跨境 ETF，避免基准被低波资产稀释。")
    yrs = m_all["years"]
    print(f"  4. 样本 {yrs:.2f} 年；超额要按 t = 夏普 × √年数 判读 —— "
          f"t=2 需要超额夏普 ≈ {2 / np.sqrt(yrs):.2f}。")
    print("  5. 分类靠名称关键词，可能漏分（如简称不带'红利'二字）；"
          "数字只用于定方向，不用于下最终结论。")
    print("  6. 本探针只读，不写任何产物。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
