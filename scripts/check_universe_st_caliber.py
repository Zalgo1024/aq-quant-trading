#!/usr/bin/env python
"""股票池口径自检（入库不变量）：默认口径不变 + 时变 ST 开关生效。

背景：2026-09-18 给 ``UniverseSelector.select`` 加了两个显式开关
（``st_mode="daily"`` / ``include_delisted=True``），用于阶段 1b 的**无偏面板**。
主链路默认行为**必须保持不变**（既有结论才可比），本脚本把这条钉住。

检查项
------
1. 默认口径（``st_mode="snapshot"``，``include_delisted=False``）：
   池中**不得出现**任何退市代码（回归保护）。
2. 逐日 ST 口径（``st_mode="daily"``）能识别「当年非 ST、现在 ST」的股票
   → 与快照口径的差集里应出现这类股票（双向偏差的第 ① 类）。
3. ``include_delisted=True`` 时退市股能进入候选（当 ``st_flags`` 覆盖足够时）。
4. 缺 ST 标记文件时回退快照规则，且计数可见（``_last_st_missing``）。

用法::

    python scripts/check_universe_st_caliber.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from aq.config.settings import PROJECT_ROOT, load_settings
from aq.data.universe import UniverseSelector

ASOF = "2024-12-31"
MIN_TURNOVER = 2e7


def main() -> int:
    cfg = load_settings()
    dl = pd.read_parquet(PROJECT_ROOT / "data_cache" / "delisted_list.parquet")
    delisted = set(dl["code"].astype(str))

    # 1) 默认口径：不得含退市代码
    base = UniverseSelector(cfg).select(index="all", asof=ASOF, min_turnover=MIN_TURNOVER)
    leak = sorted(set(base) & delisted)
    print(f"[1] 默认口径池 = {len(base)} 只 | 混入退市代码 = {len(leak)} 只 "
          f"{'✅' if not leak else '❌ ' + str(leak[:5])}")

    # 2) 逐日 ST vs 快照：差集 = 快照口径的双向错误
    #    （留 = 当年 ST 却被保留；剔 = 现在 ST 但当年正常却被整段剔除）
    s_daily = UniverseSelector(cfg)
    daily = s_daily.select(index="all", asof=ASOF, min_turnover=MIN_TURNOVER,
                           st_mode="daily")
    gain = sorted(set(daily) - set(base))
    lost = sorted(set(base) - set(daily))
    print(f"[2] 逐日口径池 = {len(daily)} 只 | 相对默认 +{len(gain)} / -{len(lost)}")
    print(f"    缺 ST 标记回退快照 = {s_daily._last_st_missing} 只"
          f"（st_flags 未覆盖时如此，覆盖后应→0）")
    print(f"    + 新进 = 快照口径把「当年正常、现在 ST」整段剔除的：{gain[:8]}")
    print(f"    - 剔出 = 快照口径错误保留的「当年 ST」：{lost[:8]}")

    # 3) ✅ 主因：主池 bars/ 里**没有**退市股（这才是幸存者偏差的来源）
    bars = PROJECT_ROOT / "data_cache" / "bars"
    in_bars = sorted(c for c in delisted if (bars / f"{c}.parquet").exists())
    print(f"[3] 退市代码出现在主池 bars/ 的 = {len(in_bars)} 只 "
          f"{'✅（预期 0）' if not in_bars else '❌ ' + str(in_bars[:5])}")
    print("    → 主池缺退市股 = 幸存者偏差的**主因**；"
          "修它靠 include_delisted=True（阶段 1b 的 306 只退市日线）")

    # 4) 含退市池（逐日 ST）
    s_all = UniverseSelector(cfg)
    allp = s_all.select(index="all", asof=ASOF, min_turnover=MIN_TURNOVER,
                        include_delisted=True, st_mode="daily")
    n_del_in = len(set(allp) & delisted)
    print(f"[4] 含退市池（逐日 ST）= {len(allp)} 只 | 池内退市股 = {n_del_in} 只")
    if n_del_in == 0:
        print("    ⚠️ 池内无退市股：可能是 st_flags 未覆盖 / 退市股当日流动性不达标"
              " → 不算失败，但需在文档中登记为缺口")

    # 5) 对照：含退市池但用快照 ST → 退市股被名称规则挡掉
    s_snap = UniverseSelector(cfg)
    snap_all = s_snap.select(index="all", asof=ASOF, min_turnover=MIN_TURNOVER,
                             include_delisted=True)
    n_del_snap = len(set(snap_all) & delisted)
    print(f"[5] 含退市池（快照 ST）= {len(snap_all)} 只 | 池内退市股 = {n_del_snap} 只"
          f" | 两口径差 = {len(set(allp) - set(snap_all))}")
    print("    ⚠️ 快照的 `contains(\"ST|退\")` 既漏过「PT水仙/邯郸钢铁」这类名字，"
          "又整体挡掉带「退」的 → 退市池必须配 st_mode=\"daily\"")

    ok = (not leak) and len(gain) > 0
    print("\n>>>", "✅ 自检通过（默认口径无泄漏 + 时变 ST 生效）" if ok else "❌ 自检失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
