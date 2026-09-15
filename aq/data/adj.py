"""复权因子的分段常数化（除权日检测 + 段内取中位数）。

问题
----
``adj_factor = 后复权收盘 / 真实收盘``。理论上是**只在除权日跳变的分段常数**，
但实测有 170/5218 只股票跳变了上百次（最多 1058 次），全是噪声：

    t          close_hfq  close_raw   adj
    2019-06-21   5.40       5.24    1.030534
    2019-06-24   5.35       5.20    1.028846   ← 只差 0.16%，不可能是除权

原因是新浪的**后复权价和真实价都只保留 2 位小数**，"分"这个最小单位
对 3 元股就是 0.33% 的量化误差，远超「真实除权」与「噪声」的分界线。
后果：``close_raw = close / adj_factor`` 每天抖，撮合价、涨跌停价跟着抖。

解法
----
1. **变点检测**：相邻比值相对变化 > ``tol``（默认 1%）才认为是真除权；
2. **段内取中位数**：同一段内的噪声对称分布在真值两侧，中位数是最优估计；
3. 段与段之间保持阶跃 —— 保住"分段常数"这个本质属性。

为什么是 1%：
- 噪声上界 ≈ 0.005 / 价格，1.5 元股也才 0.33%，10 元股 0.05% —— 1% 足够覆盖；
- A 股单次分红送转导致的 adj 跳变通常 > 1%（股息率 1% 已是偏低水平）。
  不足 1% 的极小额分红会被吸附掉，引入 ≤1% 的真实价偏差，
  权衡之下远优于每天抖动。

已知残余误差：后复权价本身的 2 位小数取整仍会给 ``close_raw`` 带来约
±0.2%（低价股）的噪声。它是**零均值随机误差**，在数千笔成交上会抵消，
量级也小于滑点模型（千一），故可接受。要彻底消除需直接存不复权 OHLC
（改动 ``Bar`` 模型 + 重拉全市场），列为 P1 待办。
"""

from __future__ import annotations

from typing import Sequence

DEFAULT_TOL = 0.01


def smooth_adj_factor(values: Sequence[float], tol: float = DEFAULT_TOL) -> list[float]:
    """把复权因子序列化为分段常数。

    Parameters
    ----------
    values:
        原始 ``adj_factor`` 序列（按时间升序）。
    tol:
        判定为「真除权」的相对变化阈值，默认 1%。

    Returns
    -------
    与输入等长的列表，段内恒定、段间断崖。
    """
    n = len(values)
    out = [0.0] * n
    if n == 0:
        return out

    def _clean(v) -> float:  # type: ignore[no-untyped-def]
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return f if f > 0 else 0.0

    i = 0
    while i < n:
        start = _clean(values[i]) or 1.0
        j = i + 1
        while j < n:
            w = _clean(values[j]) or start
            if abs(w - start) > start * tol:
                break
            j += 1

        seg = [_clean(v) for v in values[i:j]]
        seg = [v for v in seg if v > 0]
        if seg:
            seg.sort()
            mid = len(seg) // 2
            m = seg[mid] if len(seg) % 2 else (seg[mid - 1] + seg[mid]) / 2.0
        else:
            m = start
        for k in range(i, j):
            out[k] = m
        i = j

    return out


def count_jumps(values: Sequence[float], tol: float = 0.001) -> int:
    """统计跳变次数（质检用）。"""
    prev = None
    n = 0
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f <= 0:
            continue
        if prev is not None and abs(f - prev) > prev * tol:
            n += 1
        prev = f
    return n
