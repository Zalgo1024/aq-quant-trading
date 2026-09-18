#!/usr/bin/env python
"""探针：`bars/` 里 `limit_up/limit_down` 字段到底有多不可信？（物理约束定罪）

背景（2026-09-18 发现）
----------------------
`aq/data/akshare_provider.py:233` 调用 ``limit_prices(pre_close_raw, symbol, is_st=is_st)``，
其中 ``is_st`` 来自 ``self._is_st_map``（由**当前股票名称**快照得到，见第 84/111 行）。
⇒ **整段历史**（早至 2004 年）的涨跌停价，是按「**今天**是不是 ST」算出来的。

另有 ``aq/core/rules.py:112``：``pct = ST_LIMIT_PCT if is_st else LIMIT_PCT[board]``
→ **任意板块**的 ST 都被压成 5%；但科创板、以及 2020-08-24 起的创业板，
风险警示股票涨跌幅仍是 20%（交易所规则）。

判据：用「物理约束」当事实锚，而不是互相比对
--------------------------------------------
真实日收益率**不可能**超过真实涨跌幅限制。于是对每个股票日：

    |ret_adj| > 记录幅度 + 1pp   ⇒  记录幅度**可证明是错的**

其中 ``ret_adj = close / close.shift(1) - 1``（后复权序列，除权日连续，
不会产生假跳空——这正是项目采用后复权的理由）。
用 1pp 容差是为了让结论**保守**（只在明确越界时才计数）。

基准（外部真相）用已有 761 个 baostock `st_flags` 文件的逐日 `isST`。

用法::

    python scripts/probes/probe_limit_field_bias.py
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
EXCESS = 0.01  # 越界 1 个百分点才算「可证明错」，保守


def board_of(code: str) -> str:
    if code.startswith(("688", "689")):
        return "STAR"
    if code.startswith(("300", "301")):
        return "CHINEXT"
    if code.startswith(("4", "8", "92")):
        return "BSE"
    return "MAIN"


def true_band(board: str, d: pd.Timestamp, is_st: bool) -> float:
    """交易所规则的**正确**幅度（用于对照，非判据）。"""
    if board == "MAIN":
        return 0.05 if is_st else 0.10
    if board == "CHINEXT":
        if d < pd.Timestamp("2020-08-24"):
            return 0.05 if is_st else 0.10
        return 0.20                      # 注册制后 ST 也 20%
    if board == "STAR":
        return 0.20                      # 科创板 ST 也是 20%
    return 0.30


def rec_band(up_r: float, dn_r: float) -> float:
    """从记录的涨/跌停价比值（≈1+p / ≈1-p）反推幅度（取最近候选）。

    ⚠️ 必须先减去 1 再与候选**百分点**比 —— 直接拿比值 ≈1.05 去和 0.05 比，
    误差恒为 ~1.0，`<=0.02` 永不成立，全部行变 NaN，后续统计被静默吞成 0
    （2026-09-18 实踩：第一版就这样，输出了满屏「0 越界」，差点误判成没问题）。
    """
    e = np.abs((up_r - 1.0) - CANDS) + np.abs((1.0 - dn_r) - CANDS)
    i = int(np.nanargmin(np.where(np.isnan(e), np.inf, e)))
    return float(CANDS[i]) if e[i] <= 0.02 else float("nan")


def main() -> int:
    files = sorted(ST.glob("*.parquet"))
    print(f"外部真相（baostock st_flags）文件数 = {len(files)}")
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
            b = pd.read_parquet(bp, columns=["time", "close", "pre_close",
                                             "limit_up", "limit_down", "adj_factor"])
            s = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            continue
        b["d"] = pd.to_datetime(b["time"]).dt.normalize()
        s["d"] = pd.to_datetime(s["time"]).dt.normalize()
        m = (b.sort_values("d")
              .merge(s[["d", "is_st"]], on="d", how="inner")
              .reset_index(drop=True))
        m = m.dropna(subset=["pre_close", "limit_up", "limit_down", "adj_factor", "close"])
        if len(m) < 30:
            continue
        m["ret"] = m["close"] / m["close"].shift(1) - 1
        m = m.iloc[1:]                                    # 首行无收益
        pc_raw = m["pre_close"] / m["adj_factor"]
        m = m[pc_raw >= 1.0]                              # 滤掉取整误差大的低价股
        if m.empty:
            continue
        up_r = (m["limit_up"] / m["pre_close"]).to_numpy()
        dn_r = (m["limit_down"] / m["pre_close"]).to_numpy()
        ok_r = ~(np.isnan(up_r) | np.isnan(dn_r))
        if ok_r.sum() < 30:
            continue
        m = m[ok_r]
        up_r, dn_r = up_r[ok_r], dn_r[ok_r]
        rb = np.array([rec_band(u, d) for u, d in zip(up_r, dn_r)])
        bd = board_of(code)
        tb = np.array([true_band(bd, d, bool(v)) for d, v in zip(m["d"], m["is_st"])])
        ret = m["ret"].to_numpy()
        recs.append(pd.DataFrame({
            "code": code, "src": src, "board": bd, "d": m["d"].to_numpy(),
            "is_st_true": m["is_st"].to_numpy(),
            "rec_band": rb, "true_band": tb, "ret": ret,
        }))

    if not recs:
        print("无样本")
        return 1
    a = pd.concat(recs, ignore_index=True)
    print(f"样本 = {len(a)} 股票日 | 股票 = {a['code'].nunique()} 只"
          f" | 区间 {a['d'].min().date()} ~ {a['d'].max().date()}")

    # ---- 自我闸门：rec_band 若大量 NaN，说明反推逻辑坏了，必须拒绝出结论 ----
    nan_rate = float(a["rec_band"].isna().mean())
    print(f"\n[闸门] 记录幅度反推失败(NaN)率 = {nan_rate:.3%}"
          f" | 反推到的幅度分布 = {a['rec_band'].value_counts().to_dict()}")
    if nan_rate > 0.05:
        print("  ❌ 反推失败率过高 —— 反推逻辑或数据有问题，本结果**不可引用**，先查原因。")
        return 2

    # ---------- 1) 物理约束定罪 ----------
    a["viol"] = np.abs(a["ret"]) > (a["rec_band"] + EXCESS)
    print("\n【1】物理约束定罪：|实际收益| > 记录幅度 + 1pp（记录幅度可证明错）")
    print(f"  越界股票日 = {int(a['viol'].sum())} / {len(a)} = {a['viol'].mean():.3%}")
    v = a[a["viol"]]
    if not v.empty:
        print(f"  涉及股票 = {v['code'].nunique()} 只")
        print("  按记录幅度：")
        for k, g in v.groupby("rec_band"):
            print(f"    rec_band={k:.2f}: {len(g):>6d} 行 | 实际|ret| p50={g['ret'].abs().median():.2%} "
                  f"max={g['ret'].abs().max():.2%} | 其中 baostock isST=0 占 {1-g['is_st_true'].mean():.1%}")
        print("  越界最多的 8 只：")
        print(v.groupby("code").agg(n=("viol", "size"),
                                    rec=("rec_band", "first"),
                                    st_ratio=("is_st_true", "mean")).sort_values("n", ascending=False).head(8).to_string())

    # ---------- 2) 记录幅度 vs 正确幅度 ----------
    a["band_wrong"] = (~a["rec_band"].isna()) & (np.abs(a["rec_band"] - a["true_band"]) > 1e-9)
    print("\n【2】记录幅度 ≠ 交易所正确幅度（含「5% 压到创业板/科创板」规则错）")
    print(f"  错配股票日 = {int(a['band_wrong'].sum())} / {len(a)} = {a['band_wrong'].mean():.3%}")
    for bd, g in a.groupby("board"):
        w = g["band_wrong"].mean()
        print(f"    {bd:8s} 行={len(g):>7d} 错配={w:.3%}"
              f" | 该板块 ST 股票日={int(g['is_st_true'].sum()):>6d}")
    w = a[a["band_wrong"]]
    if not w.empty:
        print("  错配的 (记录→正确) 组合 top：")
        piv = (w.groupby(["rec_band", "true_band"]).size()
                 .sort_values(ascending=False).head(8))
        for (r, t), n in piv.items():
            print(f"    记录 {r:.2f} → 正确 {t:.2f} : {n} 行")

    # ---------- 3) 规则错的可证性：STAR/CHINEXT 的 ST 日 ----------
    print("\n【3】创业板/科创板 ST 日的实际波动（验证 5% 规则错）")
    for bd in ("STAR", "CHINEXT"):
        g = a[(a["board"] == bd) & (a["is_st_true"] == 1)]
        if g.empty:
            print(f"  {bd}: 无 ST 股票日样本")
            continue
        over5 = (g["ret"].abs() > 0.05 + EXCESS).mean()
        print(f"  {bd}: ST 日={len(g)} | |ret|>6% 占比={over5:.2%}"
              f" | |ret| p50={g['ret'].abs().median():.2%} p99={g['ret'].abs().quantile(0.99):.2%}"
              f" → 实际幅度显然不是 5%")

    # ---------- 4) 两个方向的规模（用 baostock 对照） ----------
    print("\n【4】双向错误的规模（主板）")
    mn = a[a["board"] == "MAIN"]
    now_st = mn.groupby("code")["is_st_true"].transform("max")
    # 方向A：整段被压 5%（当前 ST）但当年不是 ST
    A = mn[(mn["rec_band"] == 0.05) & (mn["is_st_true"] == 0)]
    print(f"  A 记录 5% 但当日非 ST = {len(A)} 行（{A['code'].nunique()} 只）"
          f"→ 这些日子的真实幅度是 10%，记录价错了")
    a_st = mn[(mn["rec_band"] == 0.10) & (mn["is_st_true"] == 1)]
    print(f"  B 记录 10% 但当日是 ST = {len(a_st)} 行（{a_st['code'].nunique()} 只）"
          f"→ 这些日子真实幅度是 5%，记录价错了")
    if not a_st.empty:
        touch = (a_st["ret"].abs() >= 0.045).mean()
        print(f"     B 组里 |ret| 落在 [4.5%,5%] 的占比 = {touch:.2%}"
              f"（若真为 5% 幅度，大量日子会贴着 5% 走）")

    # ---------- 5) 基准自检：baostock isST 是否等于「5% 幅度」 ----------
    # 若 isST=1 的日子里实际 |ret| 频繁 > 5%（物理上不可能），则 baostock 的
    # isST **不等于**交易所「涨跌幅 5% 的风险警示」口径 —— 用它做池过滤同样要打折。
    print("\n【5】基准自检：baostock isST=1 的日子，实际波动是否守 5%？（主板）")
    mn_st = a[(a["board"] == "MAIN") & (a["is_st_true"] == 1)]
    if mn_st.empty:
        print("  无样本")
    else:
        over5 = mn_st["ret"].abs() > 0.0611          # 5% 幅度 + 1.1pp 容差
        print(f"  主板 isST=1 行 = {len(mn_st)} | |ret|>6.11% 的 = {int(over5.sum())}"
              f"（{over5.mean():.2%}）")
        print(f"  |ret| 分位：p50={mn_st['ret'].abs().median():.2%}"
              f" p90={mn_st['ret'].abs().quantile(0.90):.2%}"
              f" p99={mn_st['ret'].abs().quantile(0.99):.2%}"
              f" max={mn_st['ret'].abs().max():.2%}")
        g = mn_st.groupby("code")["ret"].apply(lambda s: s.abs().max())
        bad = g[g > 0.0611]
        print(f"  按股：isST 期间 max|ret| > 6.11% 的股票 = {len(bad)} / {g.size}"
              f"（{len(bad)/g.size:.1%}）→ 这些股票的 isST 期间实际幅度显然不是 5%")
        if not bad.empty:
            print("   前 8 只：")
            bb = bad.sort_values(ascending=False).head(8)
            for c, v in bb.items():
                sub = mn_st[mn_st["code"] == c]
                print(f"     {c}: max|ret|={v:.2%} | isST 行={len(sub)}"
                      f" | 其记录幅度={sub['rec_band'].iloc[0]:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
