# -*- coding: utf-8 -*-
"""面板文件选择的公共工具。

为什么单独抽这个模块
--------------------
`data_cache/factors/` 下会同时存在**多个** ``panel_*.parquet``：
不同区间、不同股票池（``panel_liquid_*`` 是全市场流动性池，
``panel_all_*`` 是按回测区间切的小池）。它们的因子有效性**可能完全不同**。

历史教训（代价很大，勿重蹈）
----------------------------
原先三个脚本各自实现了一份"选面板"的逻辑，都是：

    sorted(glob("panel_*.parquet"), key=lambda x: x.stat().st_mtime)[-1]

即**取最近修改的那个**。而 mtime 最新的恰好是 ``panel_all_2021-01-04``
（**261 只**股票），于是：

- ``attribution_test.py`` 用它跑出了整节组合层结论，**方向与全市场相反**
  （该池上打分五档年化 Q1>Q5、单调性反转；全市场面板则严格单调递增）；
- ``neutral_diag.py`` 用它验证"因子方向"，验证对象本身就有偏。

**mtime 与"数据好坏"没有任何关系。** 面板的覆盖度（行数）才决定结论可信度，
所以这里统一按**行数降序**选最大者，并允许多候选时提示。
想用特定面板时，所有脚本都支持显式传 ``--panel``。
"""
from __future__ import annotations

from pathlib import Path

# 项目根目录（scripts/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PANEL_DIR = PROJECT_ROOT / "data_cache" / "factors"


def _row_count(p: Path) -> int:
    """读 parquet 元数据取行数（不加载数据，快且省内存）。失败返回 -1。"""
    try:
        import pyarrow.parquet as pq

        return int(pq.ParquetFile(p).metadata.num_rows)
    except Exception:  # noqa: BLE001
        return -1


def find_panel(explicit: str | Path | None = None, *, verbose: bool = True) -> Path:
    """返回要用的面板文件路径。

    Args:
        explicit: 显式指定的路径（``--panel``）。给了就直接用，并校验存在。
        verbose: 是否打印"候选 N 个，选用了谁"的提示。

    Raises:
        SystemExit: 找不到任何面板，或显式指定的路径不存在。
    """
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if not p.exists():
            raise SystemExit(f"指定的面板不存在：{p}")
        return p

    cands = list(PANEL_DIR.glob("panel_*.parquet"))
    if not cands:
        raise SystemExit(
            f"找不到面板文件（{PANEL_DIR}）。"
            f"请先运行 scripts/factor_research.py 生成。"
        )

    ranked = sorted(cands, key=lambda p: (_row_count(p), p.stat().st_size))
    best = ranked[-1]
    if verbose and len(ranked) > 1:
        print(f"[面板] 候选 {len(ranked)} 个，按覆盖度选用 {best.name}"
              f"（{_row_count(best):,} 行）；如需指定请用 --panel")
    return best
