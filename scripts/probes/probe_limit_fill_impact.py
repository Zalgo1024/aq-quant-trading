#!/usr/bin/env python
"""探针：涨跌停幅度错配到底改变了多少「封板成交判定」？（把 §9 的"预计"换成实测）

背景
----
`docs/退市股数据层与源裁决.md` §9 已证明 `bars/` 的 `limit_up/limit_down` 是
**快照 ST + 当前板块规则**算出来的，并给出错配率 10.59%。但当时对已发布回测数字的
影响只写了「**量级预计**远小于 D1 −2.31pp」—— 这是判断而非测量，必须实测。

口径
----
复刻 `aq/execution/sim_gateway.py:191-196` 的判定：买单调 whitelist
`open_raw >= limit_up_raw − 1e-6` ⇒ 不成交（涨停封板）；
`sold` 同理看 `limit_down_raw`。这里只算**涨停侧**（买单），因为跌停侧在错配
方向上是保守的（记录幅度偏大 → limit_down 偏低 → 更容易判跌停 → 更保守）。

定义 **错误放行日**：真实幅度下该股票开盘即涨停（应封板不成交），
而记录幅度下没被判为涨停（错误放行）：
    record_band > true_band  且  limit_up_true ≤ open_raw < limit_up_rec

范围（只测**影响已发布结论**的两个方向）
- 方向 C：创业板 2019-01-01 ~ 2020-08-21（注册制前真实 ±10%，记录一律 ±20%）
- 方向 B：主板 + 逐日 isST=1（真实 5%，记录常用 10%）——用 761 个 baostock 文件当样本

用法::

    python scripts/probes/probe_limit_fill_impact.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from aq.config.settings import PROJECT_ROOT

BARS = PROJECT_ROOT / "data_cache" / "bars"
DBARS = PROJECT_ROOT / "data_cache" / "delisted_bars"
ST = PROJECT_ROOT / "data_cache" / "st_flags"

CUT = pd.Timestamp("2020-08-24")     # 创业板注册制生效：此后 ST 也是 20%
START = pd.Timestamp("2019-01-01")   # 项目主口径起点


def board_of(code: str) -> str:
    if code.startswith(("688", "689")):
        return "STAR"
    if code.startswith(("300", "301")):
        return "CHINEXT"
    if code.startswith(("4", "8", "92")):
        return "BSE"
    return "MAIN"


def limit_price(pre_raw: float, code: str, is_st: bool, d: pd.Timestamp) -> float:
    """复刻 `aq/core/rules.py::limit_prices` 的涨停价，但用**正确的**时变规则。"""
    bd = board_of(code)
    if bd == "MAIN":
        p = 0.05 if is_st else 0.10
    elif bd == "CHINEXT":
        p = (0.05 if is_st else 0.10) if d < CUT else 0.20
    elif bd == "STAR":
        p = 0.20
    else:
        p = 0.30
    return round(pre_raw * (1 + p), 2)


def main() -> int:
    rows = []

    # ---------------- 方向 C：创业板 2020-08-24 之前 ----------------
    # ⚠️ glob "30*" 已同时覆盖 300*/301*，不要再叠加 "301*"（会重复计数）。
    files = sorted(set(BARS.glob("30*.parquet")))
    n_day = n_block_true = n_block_rec = n_wrong_pass = 0
    codes_c = 0
    for p in files:
        code = p.stem
        if board_of(code) != "CHINEXT":
            continue
        try:
            df = pd.read_parquet(p, columns=["time", "open", "pre_close", "adj_factor"])
        except Exception:  # noqa: BLE001
            continue
        df["d"] = pd.to_datetime(df["time"]).dt.normalize()
        df = df[(df["d"] >= START) & (df["d"] < CUT)].dropna(
            subset=["open", "pre_close", "adj_factor"])
        if df.empty:
            continue
        codes_c += 1
        o = (df["open"] / df["adj_factor"]).to_numpy()
        pc = (df["pre_close"] / df["adj_factor"]).to_numpy()
        keep = pc >= 1.0
        o, pc, ds = o[keep], pc[keep], df["d"].to_numpy()[keep]
        # 真实幅度（创业板 2020 前：非 ST 10%；这里只用"非 ST"上界，ST 属方向 B）
        lu_true = np.array([round(x * 1.10, 2) for x in pc])
        lu_rec = np.array([round(x * 1.20, 2) for x in pc])   # 记录用的是"当前板块"20%
        b_t = o >= lu_true - 1e-6
        b_r = o >= lu_rec - 1e-6
        n_day += len(o)
        n_block_true += int(b_t.sum())
        n_block_rec += int(b_r.sum())
        n_wrong_pass += int((b_t & ~b_r).sum())

    print("=" * 78)
    print("【方向 C】创业板 2019-01-01 ~ 2020-08-21（真实 ±10%，记录一律 ±20%）")
    print(f"  股票 = {codes_c} 只 | 股票日 = {n_day}")
    print(f"  真涨停日（应封板）= {n_block_true} | 记录判定封板 = {n_block_rec}"
          f" | **错误放行 = {n_wrong_pass}**")
    if n_day:
        print(f"  错误放行占比 = {n_wrong_pass / n_day:.4%}（真封板日里错放 {n_wrong_pass / max(n_block_true,1):.2%}）")
        print("  ⚠️ 这是**下界**：此处把「非 ST」当基准（10%）。若该股当时为 ST（真实 5%），"
              "错误放行还会更多。")

    # ---------------- 方向 B：主板 + 逐日 isST=1 ----------------
    st_files = sorted(ST.glob("*.parquet"))
    n_day_b = n_rec_block = n_wrong_pass_b = 0
    codes_b = 0
    for p in st_files:
        code = p.stem
        if board_of(code) != "MAIN":
            continue
        bp = BARS / f"{code}.parquet"
        if not bp.exists():
            bp = DBARS / f"{code}.parquet"
        if not bp.exists():
            continue
        try:
            b = pd.read_parquet(bp, columns=["time", "open", "pre_close", "adj_factor"])
            s = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            continue
        b["d"] = pd.to_datetime(b["time"]).dt.normalize()
        s["d"] = pd.to_datetime(s["time"]).dt.normalize()
        m = b.merge(s[["d", "is_st"]], on="d", how="inner")
        m = m[(m["d"] >= START) & (m["is_st"] == 1)].dropna(
            subset=["open", "pre_close", "adj_factor"])
        o = (m["open"] / m["adj_factor"]).to_numpy()
        pc = (m["pre_close"] / m["adj_factor"]).to_numpy()
        keep = pc >= 1.0
        o, pc = o[keep], pc[keep]
        if len(o) == 0:
            continue
        codes_b += 1
        lu_true = np.array([round(x * 1.05, 2) for x in pc])   # ST 真实 5%
        lu_rec = np.array([round(x * 1.10, 2) for x in pc])    # 记录（现摘帽）10%
        b_t = o >= lu_true - 1e-6
        b_r = o >= lu_rec - 1e-6
        n_day_b += len(o)
        n_rec_block += int(b_r.sum())
        n_wrong_pass_b += int((b_t & ~b_r).sum())

    print("=" * 78)
    print("【方向 B】主板 isST=1 期间（真实 5%，记录 10%）｜样本 = 761 只 baostock 文件")
    print(f"  股票 = {codes_b} 只 | ST 股票日 = {n_day_b}")
    print(f"  记录判定封板 = {n_rec_block} | **错误放行 = {n_wrong_pass_b}**")
    if n_day_b:
        print(f"  错误放行占 ST 股票日 = {n_wrong_pass_b / n_day_b:.4%}")

    print("=" * 78)
    print("结论读法：错误放行 = 「本该封板不成交、却被放行成交」→ 使回测收益**偏高**。")
    print("  分母口径注意：错误放行是**极稀疏事件**，必须与总股票日比，不能与封板日比。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
