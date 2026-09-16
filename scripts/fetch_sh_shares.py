# -*- coding: utf-8 -*-
"""补齐沪市股本（P2.5）。

问题
----
`data_cache/stock_list.parquet` 里 2318 只股票没有 `float_share` / `total_share`：

| 板块 | 代码段 | 缺失数 |
|---|---|---|
| 沪市主板 | `60xxxx` | 1701 |
| 科创板 | `68xxxx` | 617 |

深市（`00xxxx`/`30xxxx`）与北交所（`8xxxxx`/`4xxxxx`）齐备 —— 因为深交所
`stock_info_sz_name_code` 与北交所 `stock_info_bj_name_code` 都直接提供股本，
而**上交所 `stock_info_sh_name_code` 只返回 6 列，没有任何股本字段**。

后果
----
`mktcap = close × float_share` 对沪市股票为 NaN → 市值中性化退化为"仅行业中性化"，
只覆盖 58% 样本，**中性化收益被系统性低估**（因子研究报告 8.1 节）。

本脚本的解法
------------
用**巨潮资讯**（cninfo）的 ``stock_share_change_cninfo``：它给出每只股票的
**股本变动历史**（含 `变动日期` / `总股本` / `已流通股份`），沪市个股全部可用。

单位换算（已交叉验证）
----------------------
- 巨潮返回**万股**；
- 本地 `stock_list.parquet` 用**股**（如平安银行 `float_share = 1.9406e10`）。

故写入前需 **×10000**。标定方法：浦发银行 600000 巨潮最新总股本
2935217.8 万股 × 1e4 = 2.9352e10 股 = **293.52 亿股**，与公开数据一致。

只补不覆盖
----------
默认只填**当前为空**的行（``--force`` 可覆盖）。已有数据（深市/北交所）
一律不动 —— 那些值是交易所官方口径，不该被第三方源替换。

跑法
----
    python scripts/fetch_sh_shares.py              # 只补缺失的沪市股票
    python scripts/fetch_sh_shares.py --dry-run    # 只看会改多少行，不落盘
    python scripts/fetch_sh_shares.py --limit 20   # 小样本试跑
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

STOCK_LIST = PROJECT_ROOT / "data_cache" / "stock_list.parquet"

#: 巨潮返回的是「万股」，本地口径是「股」
WAN_TO_SHARE = 10_000.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="补齐沪市股本（巨潮源）")
    p.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    p.add_argument("--force", action="store_true",
                   help="覆盖已有值（默认只填空值；已有数据来自交易所官方口径，不建议覆盖）")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 只（试跑用）")
    p.add_argument("--sleep", type=float, default=0.35,
                   help="每只之间的间隔（秒），避免被限流")
    return p.parse_args(argv)


def _num(v) -> float | None:
    """转 float，无效值（含 0 / 负 / NaN / 非数）返回 None。

    **必须把 0 也算无效**：曾出现过 `total_share=0` 的脏数据进回归，
    导致 `ln(0) = -inf` 污染整列系数（因子面板里已加同样校验）。
    """
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not pd.notna(x) or x <= 0:
        return None
    return x


def fetch_one(symbol: str) -> tuple[float, float] | None:
    """取单只股票**最新**一期的（总股本, 流通股本），单位：股。

    巨潮返回的是一张「变动历史」表，取 `变动日期` 最大的那一行 ——
    即当前有效股本。历史序列本轮用不到（中性化用的是当期市值），
    但保留取最新的逻辑以免误用旧值。
    """
    import akshare as ak

    df = ak.stock_share_change_cninfo(symbol=symbol)
    if df is None or df.empty:
        return None
    if "变动日期" not in df.columns:
        return None
    df = df.copy()
    df["变动日期"] = pd.to_datetime(df["变动日期"], errors="coerce")
    df = df.dropna(subset=["变动日期"]).sort_values("变动日期")
    if df.empty:
        return None
    row = df.iloc[-1]

    total = _num(row.get("总股本"))
    # 巨潮的「已流通股份」即流通股本。缺失时退回总股本（中性化宁可粗也不要 NaN）
    free = _num(row.get("已流通股份")) or total
    if total is None and free is None:
        return None
    total = total if total is not None else free
    free = free if free is not None else total
    return total * WAN_TO_SHARE, free * WAN_TO_SHARE


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not STOCK_LIST.exists():
        print(f"[错误] 找不到 {STOCK_LIST}")
        return 1

    df = pd.read_parquet(STOCK_LIST)
    sym = df["symbol"].astype(str).str.zfill(6)
    df["symbol"] = sym
    for c in ("float_share", "total_share"):
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    # 缺失 = 股本 <= 0（0 与 NaN 都算缺失，见 _num 的说明）
    need = df[(df["float_share"] <= 0) | (df["total_share"] <= 0)]
    # 只处理沪市：深市/北交所有交易所官方数据，不该被第三方源覆盖
    need = need[need["symbol"].str.match(r"^(60|68)")]
    targets = need["symbol"].tolist()
    if args.limit:
        targets = targets[: args.limit]

    print(f"[范围] 总 {len(df)} 只，股本缺失 {len(need)} 只（沪市 60xxxx/68xxxx）")
    print(f"[范围] 本次处理 {len(targets)} 只" + ("（--limit 截断）" if args.limit else ""))
    if not targets:
        print("[完成] 没有需要补齐的股票")
        return 0

    t0 = time.time()
    ok = 0
    fail: list[str] = []
    updates: dict[str, tuple[float, float]] = {}
    for i, s in enumerate(targets, 1):
        try:
            got = fetch_one(s)
        except Exception as exc:  # noqa: BLE001
            got = None
            if len(fail) < 5:
                print(f"  [失败] {s}: {type(exc).__name__} {str(exc)[:80]}")
        if got is None:
            fail.append(s)
        else:
            updates[s] = got
            ok += 1
        if i % 100 == 0 or i == len(targets):
            print(f"  ... {i}/{len(targets)} 成功={ok} 失败={len(fail)} "
                  f"用时 {time.time()-t0:.0f}s")
        time.sleep(args.sleep)

    print(f"\n[结果] 取得股本 {ok} 只，失败 {len(fail)} 只，"
          f"用时 {time.time()-t0:.0f}s")
    if fail:
        preview = ", ".join(fail[:10])
        print(f"       失败样例：{preview}" + (" ..." if len(fail) > 10 else ""))

    if not updates:
        print("[完成] 无有效数据，未修改文件")
        return 0

    # 写回：只填空值（--force 才覆盖）
    n_fill = 0
    for s, (total, free) in updates.items():
        m = df["symbol"] == s
        if not m.any():
            continue
        idx = df.index[m][0]
        if args.force or float(df.at[idx, "total_share"]) <= 0:
            df.at[idx, "total_share"] = total
        if args.force or float(df.at[idx, "float_share"]) <= 0:
            df.at[idx, "float_share"] = free
        n_fill += 1

    covered = int(((df["float_share"] > 0) & (df["total_share"] > 0)).sum())
    print(f"[覆盖] 写入 {n_fill} 只 → 股本齐备 {covered}/{len(df)} "
          f"（{covered/len(df):.1%}，修复前 3244/{len(df)} = 58.3%）")

    if args.dry_run:
        print("[dry-run] 未写入文件")
        return 0

    df.to_parquet(STOCK_LIST, index=False)
    print(f"[落盘] {STOCK_LIST.relative_to(PROJECT_ROOT)}"
          f"（{datetime.now().isoformat(timespec='seconds')}）")
    print("\n[提示] 面板里的 mktcap 是构建期算出的，需重建面板才生效：")
    print("       python scripts/factor_research.py --start 2019-01-01 "
          "--end 2026-09-15 --neutralize --force --outdir runtime/factor_research/full_neu")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
