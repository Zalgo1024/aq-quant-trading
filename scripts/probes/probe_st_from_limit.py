#!/usr/bin/env python
"""探针：能否用「自己行情里的涨跌停价」反推逐日 ST 状态？（自包含替代源可行性）

动机
----
阶段 1b 需要**逐日** ST 状态，但当前两条路都不通：
- baostock `isST` 是唯一直接给出逐日 ST 的源，可多进程并发触发了 IP 封禁
  （`10001011 黑名单用户`），且封禁后查询**静默返回空** —— 当下阻塞；
- akshare `stock_info_change_name` **只返回名称、不返回日期**（已实测：
  000503 → [琼海虹, 海虹控股, ST海虹, ...] 共 7 个名称，无变更日期），
  无法还原「哪段期间是 ST」。

但主行情 `bars/` 自带 `limit_up/limit_down/pre_close`（实测 600000 填充 2112/2113）。
A 股涨跌幅限制与 ST 状态强相关：

========  =======================  =============================  ==========
板块      正常                     风险警示(ST/*ST)               可区分?
========  =======================  =============================  ==========
沪深主板  ±10%                     ±5%                            ✅ 可
创业板    ±10%（2020-08-24 前）     ±5%（2020-08-24 前）            ✅ 可
创业板    ±20%（2020-08-24 起）     ±20%（2020-08-24 起）           ❌ 不可
科创板    ±20%                     ±20%                           ❌ 不可
北交所    ±30%                     ±30%                           ❌ 不可
========  =======================  =============================  ==========

本探针以**已有的 761 个 baostock st_flags 文件为基准**，检验推断准确率，
判断它能否当自包含替代源（并明确边界：哪些板块/时段测不出来）。

方法
----
对每一行：``r_up = limit_up / pre_close``、``r_dn = limit_down / pre_close``。
两个价格都在**后复权空间**且共用同一 ``adj_factor`` → 比值尺度无关，直接等于
真实比值（受「真实价四舍五入到 0.01」影响，偏差 ≤ 0.005/真实价）。
在候选 {0.05, 0.10, 0.20, 0.30} 里选使 ``|r_up-(1+p)| + |(1-p)-r_dn|`` 最小者，
则 ``p==0.05`` ⇒ ST。

只评估 `pre_close/adj_factor ≥ 1.0` 的行（低价股 0.01 元取整误差会污染判据）。

结论（2026-09-18，**此路不通，已废弃**）
------------------------------------
实测准确率看似 95%，但那是多数类堆出来的假象：**精确率仅 18.43%、召回率 43.81%**
（TP=11082 vs FP=49037）。原因在读码后找到：
`aq/data/akshare_provider.py:233` 的 `limit_up/limit_down` 是**我们自己算出来的**
（`limit_prices(pre_close_raw, symbol, is_st=is_st)`），而 `is_st` 取自**当前名称快照**
→ 用它反推 ST 等于**把快照标志又读了一遍**，是循环论证。

⇒ 保留本脚本仅作「此路不通」的登记，不要再尝试从涨跌停价推断 ST。
⇒ 真正的替代结论见 `probe_limit_field_bias.py`：该字段本身**是错的**（可证明），
   而 baostock 的 `isST` 经裁决确为「5% 幅度风险警示」口径。

用法::

    python scripts/probes/probe_st_from_limit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from aq.config.settings import PROJECT_ROOT

ST = PROJECT_ROOT / "data_cache" / "st_flags"
BARS = PROJECT_ROOT / "data_cache" / "bars"
DBARS = PROJECT_ROOT / "data_cache" / "delisted_bars"

CANDS = np.array([0.05, 0.10, 0.20, 0.30])
TOL = 0.012  # r_up 与 1+p 的容差（覆盖 0.01 元取整 + float 噪声）


def board_of(code: str) -> str:
    if code.startswith(("688", "689")):
        return "STAR"
    if code.startswith(("300", "301")):
        return "CHINEXT"
    if code.startswith(("4", "8", "92")):
        return "BSE"
    return "MAIN"


def infer_pct(up: np.ndarray, dn: np.ndarray) -> np.ndarray:
    """返回每行最可能的涨跌幅限制 p（NaN=不可判）。"""
    # up/dn 为 r_up-1 与 1-r_dn；对每个候选 p 算误差 |up-p| + |dn-p|，取最小
    err = np.abs(up[:, None] - CANDS) + np.abs(dn[:, None] - CANDS)  # (n, k)
    idx = np.nanargmin(np.where(np.isnan(err), np.inf, err), axis=1)
    best = CANDS[idx]
    beste = err[np.arange(len(idx)), idx]
    best = np.where(beste > 2 * TOL, np.nan, best)
    return best


def main() -> int:
    files = sorted(ST.glob("*.parquet"))
    print(f"基准（baostock st_flags）文件数 = {len(files)}")
    if not files:
        return 1

    recs = []
    for p in files:
        code = p.stem
        bp = BARS / f"{code}.parquet"
        src = "bars"
        if not bp.exists():
            bp = DBARS / f"{code}.parquet"
            src = "delisted"
        if not bp.exists():
            continue
        try:
            st = pd.read_parquet(p)
            b = pd.read_parquet(bp, columns=["time", "pre_close", "limit_up",
                                             "limit_down", "adj_factor"])
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] {code}: {type(exc).__name__} {exc}")
            continue
        st["d"] = pd.to_datetime(st["time"]).dt.normalize()
        b["d"] = pd.to_datetime(b["time"]).dt.normalize()
        m = b.merge(st[["d", "is_st"]], on="d", how="inner")
        m = m.dropna(subset=["pre_close", "limit_up", "limit_down", "adj_factor"])
        if m.empty:
            continue
        pc_raw = m["pre_close"] / m["adj_factor"]
        ok = pc_raw >= 1.0
        m = m[ok]
        if m.empty:
            continue
        r_up = (m["limit_up"] / m["pre_close"]).to_numpy()
        r_dn = (m["limit_down"] / m["pre_close"]).to_numpy()
        pct = infer_pct(r_up - 1.0, 1.0 - r_dn)
        is_st_hat = np.where(np.isnan(pct), np.nan, (np.abs(pct - 0.05) < 1e-9).astype(float))
        df = pd.DataFrame({
            "code": code,
            "src": src,
            "board": board_of(code),
            "d": m["d"].to_numpy(),
            "pct": pct,
            "is_st_true": m["is_st"].to_numpy().astype(float),
            "is_st_hat": is_st_hat,
        })
        recs.append(df)

    if not recs:
        print("无可用样本")
        return 1
    all_ = pd.concat(recs, ignore_index=True)
    print(f"样本 = {len(all_)} 行 | 覆盖股票 = {all_['code'].nunique()} 只"
          f" | 区间 {all_['d'].min().date()} ~ {all_['d'].max().date()}")

    ev = all_.dropna(subset=["is_st_hat"]).copy()
    print(f"可判行 = {len(ev)}（{len(ev)/len(all_):.1%}）| 不可判 = {len(all_)-len(ev)}")
    if ev.empty:
        return 1

    ev["hat"] = ev["is_st_hat"].astype(int)
    ev["tru"] = ev["is_st_true"].astype(int)
    tp = int(((ev.hat == 1) & (ev.tru == 1)).sum())
    fp = int(((ev.hat == 1) & (ev.tru == 0)).sum())
    fn = int(((ev.hat == 0) & (ev.tru == 1)).sum())
    tn = int(((ev.hat == 0) & (ev.tru == 0)).sum())
    acc = (tp + tn) / len(ev)
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    print(f"\n【总体】TP={tp} FP={fp} FN={fn} TN={tn} | 准确率={acc:.4%}"
          f" | 精确率={prec:.2%} 召回率={rec:.2%}")
    print(f"  基准 ST 行占比 = {ev.tru.mean():.3%}（极不平衡，务必看召回率）")

    print("\n【按板块】")
    for bd, g in ev.groupby("board"):
        t = int(((g.hat == 1) & (g.tru == 1)).sum())
        f = int(((g.hat == 1) & (g.tru == 0)).sum())
        n = int(((g.hat == 0) & (g.tru == 1)).sum())
        acc_b = ((g.hat == g.tru).mean())
        print(f"  {bd:8s} 行={len(g):>7d} 准确={acc_b:.4%} "
              f"ST行={int(g.tru.sum()):>6d} 命中={t:>6d} 误报={f:>5d} 漏报={n:>5d}")

    # 创业板分时段（2020-08-24 注册制前后）
    cx = ev[ev.board == "CHINEXT"].copy()
    if not cx.empty:
        cut = pd.Timestamp("2020-08-24")
        cx["era"] = np.where(cx["d"] < cut, "创业板<2020-08-24", "创业板>=2020-08-24")
        print("\n【创业板分时段】")
        for era, g in cx.groupby("era"):
            t = int(((g.hat == 1) & (g.tru == 1)).sum())
            n = int(((g.hat == 0) & (g.tru == 1)).sum())
            print(f"  {era:22s} 行={len(g):>7d} 准确={(g.hat==g.tru).mean():.4%} "
                  f"ST行={int(g.tru.sum()):>6d} 命中={t:>6d} 漏报={n:>5d}")

    # 漏报集中在哪些 pct 上（判断是否就是 20% 那一类）
    miss = ev[(ev.hat == 0) & (ev.tru == 1)]
    if not miss.empty:
        print("\n【漏报行的推断 pct 分布】")
        print(miss["pct"].value_counts(dropna=False).to_string())
        print("  漏报前 8 只：")
        print(miss.groupby("code").size().sort_values(ascending=False).head(8).to_string())
    fp_rows = ev[(ev.hat == 1) & (ev.tru == 0)]
    if not fp_rows.empty:
        print("\n【误报行分布】pct ->", fp_rows["pct"].value_counts().to_dict())
        print("  误报前 8 只：")
        print(fp_rows.groupby("code").size().sort_values(ascending=False).head(8).to_string())

    print("\n【推断 pct 总分布】")
    print(ev["pct"].value_counts(dropna=False).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
