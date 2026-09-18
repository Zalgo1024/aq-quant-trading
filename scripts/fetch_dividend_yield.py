# -*- coding: utf-8 -*-
"""抓取**年度股息率快照**（阶段 A 第 2 项，`docs/终局诊断与重启方案.md` §4）。

源：akshare `stock_fhps_em(date="YYYY1231")` —— 东财分红送配。
实测：2005→611 行，2024→3676 行，单年约 3 秒；字段含
``现金分红-现金分红比例`` 与 ``现金分红-股息率``。

⚠️ 三个必须先讲清的口径（改本文件前必读）
------------------------------------------
1. **单位**：``现金分红-现金分红比例`` 是**每 10 股派现（元，税前）**。
   实测锚：300033 同花顺 2023 年报 = 22.0，股息率 0.016318
   ⇒ 隐含股价 = (22.0/10)/0.016318 = **134.8 元**，与其 2023 年真实股价吻合。
   ⇒ 每股派现 = 比例 / 10。

2. ⛔ **前视偏差（本表最容易踩的坑）**：``date="20231231"`` 取的是 **2023 年报**
   的分红方案，但**预案公告日多在 2024 年 3–4 月、除权除息日更晚**。
   若把 2023 年度的股息率在 2023-01-01 就用上，等于提前半年知道年报分红。
   ⇒ 本脚本**不做**任何"按年度对齐到年初"的组装，只把原始日期列
   （预案公告日 / 股权登记日 / 除权除息日）**全部落盘**，
   由下游按 PIT 自行组装；并在自检里把"预案公告日相对年报年度的分布"打出来当警示。

3. **方案进度**：一年一只股票可能有多行（董事会预案 → 股东大会通过 → 实施分配）。
   本脚本**不去猜应该留哪行**：全部保留并带 ``progress`` 列，
   只在同一 (code, report_year) 重复时按「实施分配 > 股东大会通过 > 董事会预案」
   标一个 ``rank``，下游自行取舍。

用法
----
    python scripts/fetch_dividend_yield.py
    python scripts/fetch_dividend_yield.py --start-year 2005 --end-year 2025

落盘 ``data_cache/dividend_yield.parquet``；只读之外的唯一副作用就是写这一个文件。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve()
ROOT = HERE
while ROOT and not (ROOT / "config" / "base.yaml").exists():
    if ROOT.parent == ROOT:
        break
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from aq.data.etf_store import PROJECT_ROOT  # noqa: E402（项目根，与数据层同一处定义）

OUT_PATH = PROJECT_ROOT / "data_cache" / "dividend_yield.parquet"

# 需要的列（源列名 → 落盘列名）
COLMAP = {
    "代码": "code",
    "名称": "name",
    "现金分红-现金分红比例": "cash_per10",   # 每 10 股派现（元，税前）
    "现金分红-股息率": "div_yield",          # 小数
    "股权登记日": "record_date",
    "除权除息日": "ex_div_date",
    "预案公告日": "announce_date",
    "最新公告日期": "latest_announce_date",
    "方案进度": "progress",
}

# 方案进度的可信度排序：越大越接近"已落地"
PROGRESS_RANK = {
    "实施分配": 3,
    "实施方案": 3,
    "股东大会通过": 2,
    "董事会预案": 1,
    "不分配": 0,
    "预案": 1,
}


def _ak():
    import akshare as ak
    return ak


def fetch_year(year: int) -> pd.DataFrame:
    """抓一个年度的分红方案；失败抛异常（不静默返回空表）。"""
    ak = _ak()
    raw = ak.stock_fhps_em(date=f"{year}1231")
    if raw is None or len(raw) == 0:
        raise ValueError(f"{year} 年返回空")
    miss = [c for c in COLMAP if c not in raw.columns]
    if miss:
        raise ValueError(f"{year} 年缺列 {miss}，实际列 {list(raw.columns)}")
    out = raw[list(COLMAP)].rename(columns=COLMAP).copy()
    out["report_year"] = year
    out["code"] = out["code"].astype(str).str.zfill(6)
    for c in ("record_date", "ex_div_date", "announce_date", "latest_announce_date"):
        out[c] = pd.to_datetime(out[c], errors="coerce")
    for c in ("cash_per10", "div_yield"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["progress"] = out["progress"].astype(str).str.strip()
    out["progress_rank"] = out["progress"].map(PROGRESS_RANK).fillna(0).astype(int)
    return out


def self_check(df: pd.DataFrame) -> None:
    """落盘前自检：覆盖、前视风险、单位锚。"""
    print("\n" + "=" * 78)
    print("自检")
    print("=" * 78)

    # 1) 覆盖
    by_year = df.groupby("report_year").size()
    print("[1] 逐年行数：")
    for y, n in by_year.items():
        print(f"    {int(y)}: {int(n):>5} 行")

    # 2) ⛔ 前视风险：预案公告日相对年报年度的分布
    a = df.dropna(subset=["announce_date"])
    if len(a):
        lag = (a["announce_date"].dt.year - a["report_year"])
        print("\n[2] ⛔ 前视风险：`预案公告日`年份 − 年报年度 的分布")
        vc = lag.value_counts().sort_index()
        for k, n in vc.items():
            print(f"    +{int(k)} 年: {int(n):>6} 行 ({n / len(a):.1%})")
        late = float((lag >= 1).mean())
        print(f"    → {late:.1%} 的预案在**次年**才公告。"
              f"⇒ 按「年报年度 = 可用年度」对齐会**系统性前视约半年**，")
        print("      下游必须用 announce_date / ex_div_date 做 PIT 组装。")

    # 3) 除权除息日覆盖
    e = df.dropna(subset=["ex_div_date"])
    print(f"\n[3] 除权除息日非空：{len(e)} / {len(df)} 行 "
          f"({len(e) / max(1, len(df)):.1%}) —— 这是最推荐的 PIT 锚点")

    # 4) 单位锚：用 valuation 的 close 反算股价，验证 cash_per10/10 / price ≈ div_yield
    print("\n[4] 单位锚（cash_per10/10 ÷ 当日收盘 ≈ div_yield）：")
    vdir = PROJECT_ROOT / "data_cache" / "valuation"
    shown = 0
    for _, r in df[(df["report_year"] == 2023) & df["div_yield"].notna()
                   & (df["cash_per10"] > 0)].head(200).iterrows():
        if shown >= 5:
            break
        p = vdir / f"{r['code']}.parquet"
        if not p.exists():
            continue
        v = pd.read_parquet(p)
        anchor = pd.to_datetime(r["ex_div_date"]) if pd.notna(r["ex_div_date"]) \
            else pd.Timestamp("2023-12-31")
        v = v[v["date"] <= anchor]
        if v.empty:
            continue
        px = float(v["close"].iloc[-1])
        implied = (r["cash_per10"] / 10.0) / px
        print(f"    {r['code']} {r['name'][:8]:<10} 每10股{r['cash_per10']:>6.2f}元 "
              f"锚点价{px:>8.2f} → 反算股息率 {implied:.4%} vs 源 {r['div_yield']:.4%}")
        shown += 1
    if shown == 0:
        print("    （无可用对照，跳过）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=2005)
    ap.add_argument("--end-year", type=int, default=2025)
    ap.add_argument("--no-check", action="store_true")
    args = ap.parse_args()

    frames = []
    for y in range(args.start_year, args.end_year + 1):
        t0 = time.time()
        try:
            f = fetch_year(y)
        except Exception as e:
            print(f"  ❌ {y} 年失败：{type(e).__name__}: {str(e)[:100]}")
            continue
        frames.append(f)
        print(f"  {y} 年：{len(f)} 行（{time.time() - t0:.1f}s）")

    if not frames:
        print("!! 一个年度都没抓到")
        return 1

    df = pd.concat(frames, ignore_index=True)
    # 同一 (code, report_year) 多行时，progress_rank 高的排前面（下游取第一条即最可信）
    df = df.sort_values(["code", "report_year", "progress_rank", "latest_announce_date"],
                        ascending=[True, True, False, False]).reset_index(drop=True)
    df["symbol"] = df["code"].map(lambda c: c + (".SH" if c[0] in "56" else ".SZ"))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PATH, index=False)
    print(f"\n落盘：{OUT_PATH}（{len(df)} 行，{df['code'].nunique()} 只，"
          f"{df['report_year'].min()}–{df['report_year'].max()} 年）")

    if not args.no_check:
        self_check(df)

    print("\n口径提醒：")
    print("  1. cash_per10 = 每 10 股派现（元，税前）→ 每股派现 = cash_per10 / 10。")
    print("  2. ⛔ 别按「年报年度」直接对齐到年初用 —— 预案多在次年 3–4 月公告，会前视。")
    print("  3. 同一 (code, report_year) 可能多行（预案/通过/实施），已按 progress_rank 排序；")
    print("     下游取最可信一行或自行按 PIT 过滤。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
