#!/usr/bin/env python
"""ST 标记数据自检（入库不变量）：`data_cache/st_flags/` 是否完整可信。

为什么需要它
------------
2026-09-18 用 6 个**并发**进程抓 ST 标记时踩了两个坑：
1. baostock 对同 IP 反复 login 有风控 → 被拉黑（`10001011`）；
   被封后查询**静默返回空**（不报错），表现为「大量 ➖ 无数据」；
2. 部分文件的返回被**截断**（实测 600208/000301 停在 2012-03-28、恰好 2000 行）。
→ 因此并发期产出的文件**不可默认可信**，必须用本脚本逐只对齐行情校验。

判据
----
`st_flags/{code}.parquet` 的日期集合必须 **⊇** 该股行情（`bars/` 或
`delisted_bars/`）的日期集合 —— 行情里每个交易日都得有 ST 标记。
（st_flags 的 end 比 bars 晚几天是正常的，只做单向包含检查。）

用法::

    python scripts/check_st_flags.py            # 输出统计 + 待重拉清单
    python scripts/check_st_flags.py --write    # 同时把清单写到 runtime/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from aq.config.settings import PROJECT_ROOT

ST = PROJECT_ROOT / "data_cache" / "st_flags"
BARS = PROJECT_ROOT / "data_cache" / "bars"
DBARS = PROJECT_ROOT / "data_cache" / "delisted_bars"


def main() -> int:
    ap = argparse.ArgumentParser(description="ST 标记完整性自检")
    ap.add_argument("--write", action="store_true", help="把待重拉清单写到 runtime/")
    args = ap.parse_args()

    files = sorted(ST.glob("*.parquet"))
    print(f"st_flags 文件数 = {len(files)}")
    if not files:
        return 1

    bad_missing, bad_short, bad_hollow, ok = [], [], [], 0
    for p in files:
        code = p.stem
        bars = BARS / f"{code}.parquet"
        if not bars.exists():
            bars = DBARS / f"{code}.parquet"
        if not bars.exists():
            continue
        try:
            st = pd.read_parquet(p)
        except Exception as exc:  # noqa: BLE001
            bad_hollow.append((code, f"读失败 {type(exc).__name__}"))
            continue
        if st.empty:
            bad_hollow.append((code, "0 行"))
            continue
        st_days = set(pd.to_datetime(st["time"]).dt.date)
        bd = pd.read_parquet(bars, columns=["time"])
        bar_days = set(pd.to_datetime(bd["time"]).dt.date)
        miss = bar_days - st_days
        if miss:
            bad_missing.append((code, len(miss), min(miss), max(miss)))
            continue
        # 行数明显少于行情（>5%）也要标记（可能是其它形式的截断）
        if len(st_days) < len(bar_days) * 0.95:
            bad_short.append((code, len(st_days), len(bar_days)))
            continue
        ok += 1

    print(f"✅ 完整 = {ok} | ❌ 缺交易日 = {len(bad_missing)}"
          f" | ❌ 行数偏少 = {len(bad_short)} | ❌ 空/读失败 = {len(bad_hollow)}")
    if bad_missing:
        print("\n缺交易日（前 15）：")
        for c, n, lo, hi in bad_missing[:15]:
            print(f"  {c}: 缺 {n} 天（{lo} ~ {hi}）")
    if bad_short:
        print("\n行数偏少（前 15）：")
        for c, a, b in bad_short[:15]:
            print(f"  {c}: st {a} 行 vs 行情 {b} 天")
    if bad_hollow:
        print("\n空/读失败（前 15）：")
        for c, why in bad_hollow[:15]:
            print(f"  {c}: {why}")

    todo = sorted({c for c, *_ in bad_missing} | {c for c, *_ in bad_short}
                  | {c for c, _ in bad_hollow})
    print(f"\n待重拉合计 = {len(todo)} 只")
    if args.write and todo:
        p = PROJECT_ROOT / "runtime" / "st_retry_codes.txt"
        p.write_text("\n".join(todo), encoding="utf-8")
        print(f"清单已写：{p}")
    print("续拉命令（**必须串行，见脚本 docstring 的并发警告**）：")
    print("  python scripts/fetch_st_flags.py --symbols "
          + ",".join(todo[:5]) + ",...  （或分段 --offset/--limit）")
    return 0 if not (bad_missing or bad_short or bad_hollow) else 2


if __name__ == "__main__":
    raise SystemExit(main())
