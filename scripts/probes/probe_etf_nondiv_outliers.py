"""定位 ``verify`` 的非除息日偏离**极值落在哪一天、长什么样**。

背景
----
``EtfNavStore.verify`` 的判据是：在「累计净值比率 == 单位净值比率」的那些天
（理论上当日没有分红计入），官方日增长率必须等于单位净值增长率。
``nondiv_dev_max`` 就是这些天上的最大偏离，正常应为 **1e-4 舍入量级**。

但它是 ``max`` —— 只要有一天异常就会占满整列。实测出现过两类：
- 511880 ≈ **99.0**（疑似净值归一/份额折算，累计与单位**同步**放大 → 靠
  「两者相等」这个掩码筛不掉）；
- 510050 ≈ **0.164**（量级像一次性大额分红，但也可能是源数据跳变）。

两者都与"单位口径错误"（那种会给出整齐的 100 倍）形态不同，
所以**必须逐行看原始字段**，不能只看一个标量就归因。

本探针把最大的 N 天连同 ``时间/单位净值/累计净值/两个比率/官方日增长率``
一起打出来，并给出「非除息日偏离」在剔除了 |比率| > 15% 的极端日之后的
分布 —— 后者才是真正的入库不变量。

用法::

    python scripts/probes/probe_etf_nondiv_outliers.py
    python scripts/probes/probe_etf_nondiv_outliers.py 510050 511880
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

for _p in Path(__file__).resolve().parents:
    if (_p / "config" / "base.yaml").exists():
        ROOT = _p
        break
else:  # pragma: no cover
    raise SystemExit("找不到仓库根（config/base.yaml）")
sys.path.insert(0, str(ROOT))

from aq.data.etf_actions import NAV_RESTATE_ABS_RATIO  # noqa: E402
from aq.data.etf_store import EtfNavStore  # noqa: E402

DEFAULT = ["510050", "511880", "512100", "513100", "513500", "512890"]
#: 与生产代码**同一常量**（不要在这里另立一份 —— 判据漂移过就是这个原因）。
RESTATE_ABS_RATIO = NAV_RESTATE_ABS_RATIO


def diagnose(code: str, topn: int = 6) -> None:
    ns = EtfNavStore()
    if not ns.has(code):
        print(f"  {code} 未落盘，跳过")
        return
    n = ns.load(code)
    t = pd.to_datetime(n["time"])
    cum = pd.Series(pd.to_numeric(n["cum_nav"], errors="coerce").values, index=t)
    un = pd.Series(pd.to_numeric(n["unit_nav"], errors="coerce").values, index=t)
    rep = pd.Series(pd.to_numeric(n["nav_ret"], errors="coerce").values, index=t)
    d = pd.DataFrame({"unit_nav": un, "cum_nav": cum,
                      "cum比": cum.pct_change(), "单位比": un.pct_change(),
                      "nav_ret": rep}).dropna()
    d["掩码"] = (d["cum比"] - d["单位比"]).abs() < 1e-9
    d["偏离"] = (d["cum比"] - d["nav_ret"]).abs()
    nd = d[d["掩码"]]
    clean = nd[nd["cum比"].abs() < RESTATE_ABS_RATIO]

    print("=" * 132)
    print(f"{code}  共 {len(d)} 天 | 掩码命中 {len(nd)} 天 | "
          f"其中 |累计比| ≥ {RESTATE_ABS_RATIO} 的极端日 {len(nd) - len(clean)} 天")
    print(f"  偏离  全掩码 max = {nd['偏离'].max():.4e}  "
          f"中位 = {nd['偏离'].median():.2e}")
    if len(clean):
        print(f"  偏离  去极端 max = {clean['偏离'].max():.2e}  "
              f"中位 = {clean['偏离'].median():.2e}  99分位 = "
              f"{clean['偏离'].quantile(0.99):.2e}   ← 这才是入库不变量")
    print("-" * 132)
    show = nd.nlargest(topn, "偏离")
    print("  偏离最大的几天（原始字段）：")
    print(show.to_string(formatters={
        "unit_nav": "{:.4f}".format, "cum_nav": "{:.4f}".format,
        "cum比": "{:+.4%}".format, "单位比": "{:+.4%}".format,
        "nav_ret": "{:+.4%}".format, "偏离": "{:.4e}".format}))
    # 极端日单独看：它们是"同步缩放"，不是除息
    if len(nd) - len(clean):
        print()
        print(f"  |累计比| ≥ {RESTATE_ABS_RATIO} 的极端日（这是份额折算/净值归一，"
              f"不是除息日；掩码的 |累计比−单位比| 判据对它们无效）：")
        ex = nd[nd["cum比"].abs() >= RESTATE_ABS_RATIO]
        print(ex.to_string(formatters={
            "unit_nav": "{:.4f}".format, "cum_nav": "{:.4f}".format,
            "cum比": "{:+.4%}".format, "单位比": "{:+.4%}".format,
            "nav_ret": "{:+.4%}".format, "偏离": "{:.4e}".format}))
    print()


def main() -> int:
    codes = sys.argv[1:] or DEFAULT
    print("=" * 132)
    print("非除息日偏离的极值定位 —— 把标量还原成原始行")
    print("=" * 132)
    for c in codes:
        diagnose(c)
    print("  >>> 判读：")
    print("     · 若「去极端 max」≈ 1e-4 而「全掩码 max」很大 → 偏离完全由")
    print("       份额折算/净值归一日造成，**不是**单位口径错误 → 掩码需要排除")
    print("       这些天（本探针已给出去极端后的分布）。")
    print("     · 若「去极端 max」仍很大 → 该标的 nav_ret 与单位净值真的矛盾，")
    print("       逐只查源数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
