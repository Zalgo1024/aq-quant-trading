# -*- coding: utf-8 -*-
"""Walk-forward 前视偏差的可回归检验（纯逻辑，秒级，不依赖真实数据）。

为什么单独写一个检验
--------------------
前视偏差是**静默**类错误：切片位置错了程序照样跑完、照样出夏普，
只是这个夏普不是样本外的。7.13 的全部结论都建立在"喂给 CSCV 的日收益
是样本外的"这个前提上，而这个前提只由 walkforward.py 的几行切片保证。
所以必须有断言把它钉住，防止以后改代码时悄悄破坏。

两个层次
--------
1. **公式层**：``hi <= p - (horizon + LAG_EXTRA)``。
   其中 p 是 ``searchsorted(d)``，即第 d 日在 IC 时序里的位置。
   这个断言用**位置**（交易日计数）而非自然日表达 —— lag 本来就是
   按 index 位置算的，换成日期比较会引入噪声。
2. **因果层（黄金标准）**：把某个 refit 时点**之后**的全部 IC 数据
   替换成垃圾，重跑，断言该时点及之前的权重**一字不变**。
   这直接证明"未来数据没有泄漏进历史决策"，比任何公式推导都硬。

跑法::

    python scripts/wf_lookahead_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aq.factors.walkforward import LAG_EXTRA, WalkForwardWeighter  # noqa: E402

FACTORS = ["f_a", "f_b", "f_c", "f_d"]


# --------------------------------------------------------------------------
# 构造：合成一段"日 IC 时序"和最小可用的 scorer
# --------------------------------------------------------------------------


def _make_ic_ts(days: int = 900, seed: int = 20260916) -> pd.DataFrame:
    """造一段日 IC 时序：索引是交易日（bdate_range），列是 <因子>__rankic/__ic。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-01-04", periods=days)
    cols: dict[str, np.ndarray] = {}
    for f in FACTORS:
        # 每个因子给一点真实的自相关结构，免得统计量退化成常数
        x = rng.normal(0.02, 0.10, size=days)
        cols[f"{f}__rankic"] = np.clip(x, -0.5, 0.5)
        cols[f"{f}__ic"] = cols[f"{f}__rankic"] * 0.95
    return pd.DataFrame(cols, index=idx)


class _FakeScorer:
    """只记录"被喂了什么"，不做真实定权 —— 本检验关心的是切片的因果性。

    ⚠️ 权重必须**依赖 summary 的数值**（这里直接用 ``rank_ic``），不能只依赖
    因子名集合。第一版写成 ``1/len(names)``，于是"把未来 IC 换成垃圾"这件事
    完全不改变权重 —— 对照检验立刻暴露了它没有鉴别力（分界之后 0/17 改变，
    本该是全部改变）。**一个测不出差异的检验等于没有检验。**
    """

    def __init__(self) -> None:
        self.weights: dict[str, float] = {}
        self.calls: list[dict] = []

    def set_ic_weights(self, summary, corr=None, **kw):  # noqa: ANN001, ANN003
        names = [str(x) for x in summary["因子"]]
        vals = [float(x) for x in summary["rank_ic"]]
        self.weights = {n: v for n, v in zip(names, vals)}
        self.calls.append({
            "names": list(names),
            "n_rows": int(len(summary)),
            "corr_shape": None if corr is None else tuple(corr.shape),
        })

    def active_factors(self) -> list[str]:
        return list(self.weights)


class _Cfg:
    """最小 cfg：只要满足 walkforward 的 getattr 读取。"""

    def __init__(self, window: int, refit_every: int) -> None:
        self.model = type("M", (), {
            "wf_window": window,
            "wf_refit_every": refit_every,
            "ic_weight_mode": "icir",
            "ic_min_abs": 0.0,        # 本检验不关心显著性门控，全放行
            "ic_max_p": 1.0,
            "ic_cap": 0.25,
            "ic_shrink": 1.0,
            "ic_select": True,
            "ic_corr_threshold": 0.85,
            "ic_min_icir_ratio": 0.0,  # 同上，避免抗性门把因子清空
        })()


def _run(ic_ts: pd.DataFrame, cfg: _Cfg, dates: list[pd.Timestamp]):
    """按交易日步进，返回 (每次 refit 的记录, 用到的 IC 截止位置列表)。"""
    wf = WalkForwardWeighter(cfg, ic_ts, verbose=False, min_days=60)
    sc = _FakeScorer()
    used: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for d in dates:
        before = len(wf.history)
        wf.maybe_refit(str(d.date()), sc)
        if len(wf.history) > before:
            h = wf.history[-1]
            used.append((d, pd.Timestamp(h["ic_to"])))
    return wf, used


# --------------------------------------------------------------------------
# 检验 1：公式层
# --------------------------------------------------------------------------


def check_lag_boundary() -> bool:
    print("\n[1] 公式层：refit 用到的 IC 不得晚于 d - (horizon + LAG_EXTRA)")
    ic_ts = _make_ic_ts()
    cfg = _Cfg(window=250, refit_every=30)
    horizon = 1
    lag = horizon + LAG_EXTRA

    dates = list(ic_ts.index[300:])
    wf, used = _run(ic_ts, cfg, dates)
    if not used:
        print("    ✗ 没有任何 refit 发生，检验无效")
        return False

    pos = {d: i for i, d in enumerate(ic_ts.index)}
    bad = 0
    for d, ic_to in used:
        p = pos[d]
        # 用到的是 iloc[lo:hi]，hi 是**开区间上界** → 最晚位置 = hi - 1
        #   hi = p - lag  →  最晚位置 = p - lag - 1
        # 断言写成 <= p - lag 会多出一天松弛，等于放过了一天的前视窗口，
        # 所以这里按开区间精确表达。
        allow_last = p - lag - 1
        got_last = pos[ic_to]
        if got_last > allow_last:
            print(f"    ✗ {d.date()} 用到 IC {ic_to.date()}"
                  f"（位置 {got_last} > 允许 {allow_last}）→ 前视！")
            bad += 1

    # 同时验证"边界是紧的"：不能过度保守到丢掉一整个窗口
    tight = all(pos[ic_to] == pos[d] - lag - 1 for d, ic_to in used)
    print(f"    检查 {len(used)} 次 refit：越界 {bad} 次"
          f"（期望 0）；边界紧贴 = {tight}")
    print(f"    例：{used[0][0].date()} 用到 IC 至 {used[0][1].date()}"
          f"，{'asof 位置'} {pos[used[0][0]]}"
          f" − IC 截止位置 {pos[used[0][1]]}"
          f" = {pos[used[0][0]] - pos[used[0][1]]} 个交易日"
          f"（期望 {lag + 1} = lag({lag}) + 开区间 1）")
    return bad == 0 and tight


# --------------------------------------------------------------------------
# 检验 2：因果层 —— 扰动未来
# --------------------------------------------------------------------------


def check_causal_perturbation() -> bool:
    print("\n[2] 因果层：把某时点之后的 IC 全部换成垃圾，历史权重必须一字不变")
    ic_ts = _make_ic_ts()
    cfg = _Cfg(window=250, refit_every=30)
    dates = list(ic_ts.index[300:])

    _, used = _run(ic_ts, cfg, dates)
    if len(used) < 4:
        print("    ✗ refit 次数不足，检验无效")
        return False

    # 选第 3 个 refit 时点作为分界：它及之前必须不受未来污染影响
    cut_d = used[2][0]
    cut_pos = ic_ts.index.get_loc(cut_d)

    rng = np.random.default_rng(7)
    poisoned = ic_ts.copy()
    # 只污染"分界日及其之后"的 IC —— 注意这里从 cut_pos 开始，
    # 而 refit 本身最多只能用到 cut_pos - lag，所以即便污染从 cut_pos
    # 开始（而非 cut_pos - lag + 1），也仍然不该影响分界处的决策。
    poisoned.iloc[cut_pos:] = rng.normal(0.0, 0.5,
                                         size=(len(ic_ts) - cut_pos,
                                               ic_ts.shape[1]))

    wf_a, _ = _run(ic_ts, _Cfg(250, 30), dates)
    wf_b, used_b = _run(poisoned, _Cfg(250, 30), dates)

    n_before = sum(1 for d, _ in used_b if d <= cut_d)
    same = 0
    for h_a, h_b in zip(wf_a.history[:n_before], wf_b.history[:n_before]):
        if h_a["asof"] == h_b["asof"] and h_a["weights"] == h_b["weights"]:
            same += 1

    ok = (same == n_before) and n_before >= 3
    print(f"    分界时点 {cut_d.date()}（位置 {cut_pos}）；"
          f"分界及之前共 {n_before} 次 refit")
    print(f"    权重完全一致：{same}/{n_before}  → {'通过' if ok else '失败'}")

    # 反向对照：分界之后的决策**应该**被污染影响，否则说明检验没有鉴别力
    after_diff = sum(
        1 for h_a, h_b in zip(wf_a.history[n_before:], wf_b.history[n_before:])
        if h_a["weights"] != h_b["weights"])
    n_after = len(wf_a.history) - n_before
    print(f"    对照：分界之后 {after_diff}/{n_after} 次 refit 的权重确实改变了"
          f"  → 检验有鉴别力 = {after_diff > 0}")
    return bool(ok and (n_after == 0 or after_diff > 0))


# --------------------------------------------------------------------------
# 检验 3：refit 间隔与单调性
# --------------------------------------------------------------------------


def check_refit_cadence() -> bool:
    print("\n[3] refit 时点单调推进，且相邻间隔 >= refit_every")
    ic_ts = _make_ic_ts()
    for every in (20, 60, 120):
        cfg = _Cfg(window=250, refit_every=every)
        dates = list(ic_ts.index[300:])
        wf, _ = _run(ic_ts, cfg, dates)
        pos = [ic_ts.index.get_loc(pd.Timestamp(h["asof"])) for h in wf.history]
        mono = all(b > a for a, b in zip(pos, pos[1:]))
        gaps = [b - a for a, b in zip(pos, pos[1:])]
        min_gap = min(gaps) if gaps else 0
        ok = mono and (not gaps or min_gap >= every - 1)
        # 这里允许 -1 的松弛：hi 是按 IC 位置步进的，而 d 是按交易日步进的，
        # 两者在 lag 边界上可能差一格 —— 不松弛会得到"假失败"。
        print(f"    refit_every={every:3d}：{len(wf.history)} 次 refit，"
              f"最小间隔 {min_gap}，单调 = {mono}  → {'通过' if ok else '失败'}")
        if not ok:
            return False
    return True


def main() -> int:
    print("=" * 72)
    print("Walk-forward 前视偏差检验")
    print("=" * 72)
    r1 = check_lag_boundary()
    r2 = check_causal_perturbation()
    r3 = check_refit_cadence()
    print("\n" + "=" * 72)
    print(f"结果：公式层 {'通过' if r1 else '失败'} | "
          f"因果层 {'通过' if r2 else '失败'} | "
          f"节奏层 {'通过' if r3 else '失败'}")
    print("=" * 72)
    return 0 if (r1 and r2 and r3) else 1


if __name__ == "__main__":
    raise SystemExit(main())
