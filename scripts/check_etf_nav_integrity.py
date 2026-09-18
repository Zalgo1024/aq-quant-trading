# -*- coding: utf-8 -*-
"""ETF 净值落盘的**完整性护栏**：把"看起来有数据、其实不可用"的标的点名。

为什么需要它
------------
`EtfNavStore.total_return` 是阶段 B/C 唯一权威的全收益来源，但它有个盲区：
只要 parquet 文件在、行数够、列齐全，`load()` 就成功 —— **它不会告诉你
那一列语义是不是对的**。本脚本在数据入口之外再补一层**逐文件体检**，
把以下几类"静默不可用"点名：

1. ``RET_ALL_ZERO`` —— ``nav_ret`` 全程为 0，而 ``unit_nav`` 却在变。
   ⚠️ 2026-09-18 定性：**这不是抓取 bug，是货币 ETF 的源口径**。
   东财对货币 ETF **不给「日增长率」**（收益走「万份收益」口径），
   且 `fund_etf_fund_info_em` 塞进「单位净值」列的其实是万份收益（元），
   实测 159003 / 511600 / 511670 / 511850 的 unit_nav 在 0.13~14.6 之间跳，
   根本不是净值。⇒ **货币类必须按资产类别剔除，不能靠"零波动"兜底**
   —— 零波动只是最后一道保险，不是分类手段。
2. ``RET_ALL_NAN`` —— ``nav_ret`` 整列缺失（源没给、或解析失败）。
3. ``UNIT_CONST`` —— ``unit_nav`` 恒定（净值不动，收益无法由净值推出）。
4. ``EMPTY`` / ``TOO_FEW`` —— 文件为空或行数不足。

⚠️ 为什么"零波动"不能直接当货币判据：它是**结果**不是**原因**。
   一旦某只真权益 ETF 的源数据出问题，它也会被判成"货币"而静默出池，
   你会以为是分类正确，其实是数据坏了。所以本脚本只**报告**，不自动删。

用法
----
    python scripts/check_etf_nav_integrity.py
    python scripts/check_etf_nav_integrity.py --min-rows 60 --strict

``--strict``：存在任何缺陷时退出码 1（给 CI / 抓取后自检用）。
只读：不写任何数据产物、不改任何落盘文件。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

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

# 缺陷类别 → 说明（**计入** total_bad）
DEFECTS: dict[str, str] = {
    "EMPTY": "文件为空",
    "RET_ALL_NAN": "nav_ret 整列缺失（源没给日增长率）",
    "RET_ALL_ZERO": "nav_ret 全程为 0（实测 26/26 全是货币 ETF，源口径就是万份收益）",
    "UNIT_CONST": "unit_nav 恒定（净值不动）",
    "NO_NAV_RET_COL": "缺 nav_ret 列",
}

# 信息类别 → 说明（**不计入** total_bad：这是客观事实，不是数据坏了）
INFOS: dict[str, str] = {
    "TOO_FEW": "行数不足（绝大多数是 2025-2026 新上市，历史本来就短）",
}


def classify(df: pd.DataFrame, min_rows: int) -> str | None:
    """给单只 ETF 的净值表定性；返回 None = 健康。"""
    if df is None or len(df) == 0:
        return "EMPTY"
    if len(df) < min_rows:
        return "TOO_FEW"
    if "nav_ret" not in df.columns:
        return "NO_NAV_RET_COL"
    r = pd.to_numeric(df["nav_ret"], errors="coerce")
    u = pd.to_numeric(df.get("unit_nav"), errors="coerce") if "unit_nav" in df.columns else None
    if r.notna().sum() == 0:
        return "RET_ALL_NAN"
    if float(r.abs().max()) == 0.0:
        return "RET_ALL_ZERO"
    if u is not None and u.dropna().nunique() <= 1:
        return "UNIT_CONST"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rows", type=int, default=60)
    ap.add_argument("--strict", action="store_true", help="有任何缺陷时退出码 1")
    ap.add_argument("--show", type=int, default=15, help="每类最多列出几只")
    args = ap.parse_args()

    nav = EtfNavStore()
    codes = nav.available()
    try:
        lst = load_etf_list()
        name_of = dict(zip(lst["symbol"].astype(str), lst["name"].astype(str)))
        code_of = (dict(zip(lst["symbol"].astype(str), lst["code"].astype(str)))
                   if "code" in lst.columns else {})
    except Exception:
        name_of, code_of = {}, {}

    print("=" * 82)
    print("ETF 净值完整性护栏（只读）")
    print("=" * 82)
    print(f"净值文件        : {len(codes)} 只")
    print(f"行数下限        : {args.min_rows}")
    print()

    buckets: dict[str, list[str]] = {}
    for c in codes:
        try:
            df = nav.load(c)
        except Exception as e:
            buckets.setdefault("LOAD_FAIL", []).append(f"{c} ({type(e).__name__})")
            continue
        k = classify(df, args.min_rows)
        if k:
            buckets.setdefault(k, []).append(c)

    total_bad = 0
    for k, desc in DEFECTS.items():
        cs = buckets.get(k)
        if not cs:
            continue
        total_bad += len(cs)
        print(f"[缺陷·{k}] {len(cs)} 只 —— {desc}")
        for c in cs[:args.show]:
            nm = name_of.get(c, "")
            cd = code_of.get(c, c.split(".")[0])
            print(f"    {cd}  {c:<11} {nm}")
        if len(cs) > args.show:
            print(f"    ... 另有 {len(cs) - args.show} 只")
        print()

    lf = buckets.get("LOAD_FAIL")
    if lf:
        total_bad += len(lf)
        print(f"[缺陷·LOAD_FAIL] {len(lf)} 只 —— 读取失败")
        for c in lf[:args.show]:
            print(f"    {c}")
        print()

    for k, desc in INFOS.items():
        cs = buckets.get(k)
        if not cs:
            continue
        print(f"[信息·{k}] {len(cs)} 只 —— {desc}（不计入缺陷）")
        for c in cs[:args.show]:
            nm = name_of.get(c, "")
            cd = code_of.get(c, c.split(".")[0])
            print(f"    {cd}  {c:<11} {nm}")
        if len(cs) > args.show:
            print(f"    ... 另有 {len(cs) - args.show} 只")
        print()

    if not total_bad:
        print("✅ 未发现缺陷：全部净值文件的 nav_ret / unit_nav 语义正常")
    else:
        print(f"⚠️ 缺陷合计 {total_bad} 只 / {len(codes)} 只 "
              f"（{total_bad / max(1, len(codes)):.1%}）")

    print()
    print("定性提醒（2026-09-18）：")
    print("  · RET_ALL_ZERO 绝大多数是**货币 ETF** —— 东财不给它们日增长率，")
    print("    「单位净值」列装的其实是万份收益。⇒ 按资产类别剔除，别依赖零波动。")
    print("  · 本脚本只报告不删数据；是否出池由下游口径决定。")

    if args.strict and total_bad:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
