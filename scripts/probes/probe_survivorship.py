# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与 runtime/cscv/ 的既有结果。
# 2026-09-18 从 runtime/ 纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/（见 README「快速开始」第 5 步拉取全市场日线）。
# ---------------------------------------------------------------------------
"""幸存者偏差量化探针（只读，不写任何产物）。

> ⚠️⚠️ 口径警告（2026-09-18 发现，**未修**）：本探针用 `px = close × adj_factor`，
> 但 `bars/` 里的 `close` **本身已是后复权价**（`Bar` 约定：真实价 = close / adj_factor）
> → 等于 px = 真实价 × f²，把复权因子的跨期增长当成了收益。
> 后果：**本探针的一切绝对收益数字不可引用**（如 D1 +35.0% / D10 +61.2%），
> 且该 bug **系统性地美化最差档**（同池同窗口对照：D1−D10 被从 −20.45pp 压成 −9.88pp）。
> 定性方向（D1 跑输 D10）仍成立但幅度被低估约一半。
> **请改用 `probe_survivorship_corrected.py`**（正确口径 + 并入退市股 + 退市可实现损失）。
> 详见 `docs/退市股数据层与源裁决.md` §8。

三个问题：
  Q1「跌得多的股票」在**现存样本**里表现如何？
     退市股全部是极端下跌股 → 它们本该落在 trailing-return 的最左尾。
     如果现存样本里最左尾已经跑输，那么补上退市股只会更糟
     → 幸存者偏差在**高估**任何押注左尾（深度价值）的策略。

  Q2 低 PB（高 bp）到底是不是在捡"落下的刀子"？
     看 bp 分位 与 trailing 12M 收益分位 的相关性，
     以及 bp Top20 里"近一年跌超 30%"的比例。

  Q3 每年到底"消失"了多少只股票？（删失规模）
     用 stock_info_sh_delist / stock_info_sz_delist 数每年退市家数。

只读：不写 data_cache、不改产物。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

def _project_root() -> Path:
    """向上找到含 config/base.yaml 的目录作为项目根。

    这样脚本放在 runtime/ 还是 scripts/probes/ 都能跑对。
    """
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "config" / "base.yaml").exists():
            return cand
    return here.parents[1]


ROOT = _project_root()
BARS = ROOT / "data_cache" / "bars"
VAL = ROOT / "data_cache" / "valuation"
LIST = ROOT / "data_cache" / "stock_list.parquet"

MIN_AMOUNT = 2e7
NDEC = 10


def sec(t: str) -> None:
    print("\n" + "=" * 96)
    print(t)
    print("=" * 96)


def load_all() -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (adj_close 宽表, amount 宽表) —— date × symbol"""
    files = sorted(BARS.glob("*.parquet"))
    print(f"  读取 {len(files)} 只股票的日线 …")
    closes, amounts = [], []
    for i, p in enumerate(files):
        code = p.stem
        try:
            df = pd.read_parquet(
                p, columns=["time", "close", "amount", "adj_factor", "is_trading"]
            )
        except Exception:  # noqa: BLE001
            continue
        if df.empty:
            continue
        t = pd.to_datetime(df["time"])
        df = df.set_index(t).sort_index()
        if "is_trading" in df.columns:
            df = df[df["is_trading"] != False]  # noqa: E712
        adj = pd.to_numeric(df["adj_factor"], errors="coerce")
        px = pd.to_numeric(df["close"], errors="coerce") * adj
        closes.append(px.rename(code))
        amounts.append(pd.to_numeric(df["amount"], errors="coerce").rename(code))
        if (i + 1) % 1000 == 0:
            print(f"    …{i + 1}/{len(files)}")
    C = pd.concat(closes, axis=1).sort_index()
    A = pd.concat(amounts, axis=1).sort_index()
    return C, A


def year_ends(idx: pd.DatetimeIndex, years) -> dict[int, pd.Timestamp]:
    out = {}
    for y in years:
        sub = idx[(idx.year == y)]
        if len(sub):
            out[y] = sub[-1]
    return out


def main() -> int:
    C, A = load_all()
    print(f"  面板 {C.shape[0]} 天 × {C.shape[1]} 只，"
          f"{C.index[0].date()} ~ {C.index[-1].date()}")

    sl = pd.read_parquet(LIST)
    list_date = pd.to_datetime(sl.set_index("symbol")["list_date"], errors="coerce")

    years = list(range(2019, 2026))  # 需要前一年做 trailing，末年做 forward
    ends = year_ends(C.index, range(2018, 2027))
    sec("Q1 按「过去 12 个月收益」十分位分组 → 未来 12 个月收益（现存样本）")

    rows = []
    for y in years:
        d0 = ends.get(y - 1)
        d1 = ends.get(y + 1)
        if d0 is None or d1 is None:
            continue
        # ---- 池子：上市满 180 天 + 近 20 日日均成交额 ≥ 2e7 ----
        win = C.loc[:d0].tail(20)
        amt = A.loc[win.index].mean()
        ok_amt = amt[amt >= MIN_AMOUNT].index
        ok_age = [c for c in ok_amt
                  if c in list_date.index and pd.notna(list_date[c])
                  and (d0 - list_date[c]).days >= 180]
        pool = [c for c in ok_age if pd.notna(C.loc[d0, c]) and pd.notna(C.loc[d1, c])]
        if len(pool) < 200:
            continue
        p0 = C.loc[d0, pool]
        p1 = C.loc[d1, pool]
        pback = C.loc[:d0].iloc[-253] if C.loc[:d0].shape[0] > 253 else C.loc[:d0].iloc[0]
        p_1y_ago = pback.reindex(pool)
        trail = p0 / p_1y_ago - 1.0
        fwd = p1 / p0 - 1.0
        df = pd.DataFrame({"trail": trail, "fwd": fwd}).dropna()
        if len(df) < 200:
            continue
        df["dec"] = pd.qcut(df["trail"], NDEC, labels=False, duplicates="drop")
        g = df.groupby("dec")["fwd"].mean()
        rows.append(pd.Series(g.values, index=g.index, name=y))

    if not rows:
        print("  数据不足")
        return 1
    T = pd.DataFrame(rows)
    T.columns = [f"D{i+1}" for i in T.columns]
    print(f"\n  行=分组年份(年末)，列=过去12M收益十分位（D1=跌最多…D10=涨最多）")
    print(f"  值=**未来12个月**等权收益")
    print(T.mul(100).round(1).to_string(float_format=lambda v: f"{v:6.1f}"))
    mean = T.mean()
    print("\n  各十分位均值（%，等权平均跨年）：")
    for k, v in mean.items():
        bar = "█" * max(0, int(v * 100 / 1.5))
        print(f"    {k:<4} {v*100:7.2f}%  {bar}")
    d1 = mean.iloc[0]
    print(f"\n  判读：")
    print(f"    D1（过去一年跌最多的一档）未来12M均值 = {d1*100:+.2f}%")
    print(f"    D1 − D10 价差 = {(d1 - mean.iloc[-1])*100:+.2f} 个百分点")
    if d1 < mean.median():
        print("    → 现存样本里『跌得多』已经跑输。退市股全在更左的位置，")
        print("      补上它们只会让 D1 更差 → **幸存者偏差在系统性高估深度价值/反转策略**。")
    else:
        print("    → 现存样本里『跌得多』反而占优（反转效应）。")
        print("      但退市股会补进最左尾 → 该反转效应的真实幅度被**高估**，需要打折。")

    # ---------------- Q2 低 PB 是不是在捡落下的刀子 ----------------
    sec("Q2 高 bp（便宜）↔ 过去一年跌得多？—— 退市删失会命中谁")
    corr_rows = []
    for y in years:
        d0 = ends.get(y - 1)
        d1 = ends.get(y + 1)
        if d0 is None or d1 is None:
            continue
        win = C.loc[:d0].tail(20)
        amt = A.loc[win.index].mean()
        pool = [c for c in amt[amt >= MIN_AMOUNT].index
                if pd.notna(C.loc[d0, c]) and pd.notna(C.loc[d1, c])]
        ds = pd.Timestamp(d0).strftime("%Y-%m-%d")
        bps = {}
        for c in pool:
            vp = VAL / f"{c}.parquet"
            if not vp.exists():
                continue
            try:
                v = pd.read_parquet(vp, columns=["date", "pb"])
            except Exception:  # noqa: BLE001
                continue
            v = v[pd.to_datetime(v["date"]) <= d0]
            if v.empty:
                continue
            pb = pd.to_numeric(v["pb"].iloc[-1], errors="coerce")
            if pd.notna(pb) and pb > 0:
                bps[c] = 1.0 / pb  # bp = 1/pb
        if len(bps) < 200:
            continue
        bps = pd.Series(bps)
        pool2 = bps.index
        pback = C.loc[:d0].iloc[-253] if C.loc[:d0].shape[0] > 253 else C.loc[:d0].iloc[0]
        trail = (C.loc[d0, pool2] / pback.reindex(pool2) - 1.0).dropna()
        common = bps.index.intersection(trail.index)
        if len(common) < 200:
            continue
        rho = np.corrcoef(bps[common].rank(), trail[common].rank())[0, 1]
        # bp Top20 里近一年跌超 30% 的比例
        top20 = bps[common].nlargest(20).index
        frac = float((trail[top20] < -0.30).mean())
        corr_rows.append({"year": y, "spearman_bp_trail": rho,
                          "top20_frac_drop30": frac,
                          "top20_med_trail": float(trail[top20].median()),
                          "pool_med_trail": float(trail[common].median())})
    if corr_rows:
        R = pd.DataFrame(corr_rows).set_index("year")
        print(R.round(3).to_string())
        print(f"\n  平均 Spearman(bp, 过去12M收益) = {R['spearman_bp_trail'].mean():+.3f}")
        print(f"  bp Top20 中『近一年跌超30%』平均占比 = "
              f"{R['top20_frac_drop30'].mean()*100:.1f}%")
        print(f"  bp Top20 过去12M收益中位数 = {R['top20_med_trail'].mean()*100:+.1f}%"
              f"   全池中位数 = {R['pool_med_trail'].mean()*100:+.1f}%")
        if R["top20_med_trail"].mean() < R["pool_med_trail"].mean() - 0.05:
            print("  → **高 bp 组合确实在捡落下的刀子**：买的就是近一年大跌的票。")
            print("    退市股是这条尾巴的极端延伸 → 高 bp 组合的历史收益**被系统性高估**。")

    # ---------------- Q4 组合层面的真实拖累 ----------------
    sec("Q4 若把『近死股』算进来，组合会被拖多少？（删失的实际量级）")
    # 对每个分组年，取 bp Top20（等权组合），看未来 12M 里
    #   最惨那 1~2 只造成多少拖累，以及跌超 50% 的只数占比。
    drag_rows = []
    for y in years:
        d0 = ends.get(y - 1)
        d1 = ends.get(y + 1)
        if d0 is None or d1 is None:
            continue
        win = C.loc[:d0].tail(20)
        amt = A.loc[win.index].mean()
        pool = [c for c in amt[amt >= MIN_AMOUNT].index
                if pd.notna(C.loc[d0, c]) and pd.notna(C.loc[d1, c])]
        bps = {}
        for c in pool:
            vp = VAL / f"{c}.parquet"
            if not vp.exists():
                continue
            try:
                v = pd.read_parquet(vp, columns=["date", "pb"])
            except Exception:  # noqa: BLE001
                continue
            v = v[pd.to_datetime(v["date"]) <= d0]
            if v.empty:
                continue
            pb = pd.to_numeric(v["pb"].iloc[-1], errors="coerce")
            if pd.notna(pb) and pb > 0:
                bps[c] = 1.0 / pb
        if len(bps) < 200:
            continue
        bps = pd.Series(bps)
        top20 = bps.nlargest(20).index
        fwd = (C.loc[d1, top20] / C.loc[d0, top20] - 1.0).dropna()
        if len(fwd) < 10:
            continue
        worst1 = float(fwd.min())
        worst2 = float(fwd.nsmallest(2).mean())
        drag_rows.append({
            "year": y,
            "top20_fwd_mean": float(fwd.mean()),
            "n_lt_-50pct": int((fwd < -0.50).sum()),
            "worst1": worst1,
            "worst2_avg": worst2,
            "drag_worst1_pp": worst1 / len(fwd) * 100,
        })
    if drag_rows:
        D = pd.DataFrame(drag_rows).set_index("year")
        print(D.round(3).to_string())
        print(f"\n  bp Top20 等权未来12M收益均值（跨年） = "
              f"{D['top20_fwd_mean'].mean()*100:+.2f}%")
        print(f"  每年『跌超50%』只数（满分20）平均 = {D['n_lt_-50pct'].mean():.2f} 只"
              f"  （占组合 {D['n_lt_-50pct'].mean()/20*100:.1f}%）")
        print(f"  最惨 1 只对组合的平均拖累 = {D['drag_worst1_pp'].mean():+.2f} 个百分点")
        print(f"  最惨 2 只平均收益 = {D['worst2_avg'].mean()*100:+.1f}%")
        n50 = D["n_lt_-50pct"].mean()
        d1p = abs(D["drag_worst1_pp"].mean())
        print("\n  判读：退市股 = 这只『最惨 1 只』的极端版本（-60%~-90% 而非 -50%）。")
        print(f"  现存样本每年已经出现 ~{n50:.1f} 只 -50% 级的票（占组合 {n50/20*100:.1f}%）；")
        print("  补上退市股相当于把『最惨1只』从 -50% 换成 -80%，并额外多出几只")
        print(f"  → 年度拖累量级 ≈ {d1p*0.6:.1f} ~ {d1p*2:.1f} 个百分点。")

    # ---------------- Q3 每年消失多少只 ----------------
    sec("Q3 每年退市家数（删失规模）")
    try:
        import akshare as ak
        for name, dcol in (("stock_info_sh_delist", "暂停上市日期"),
                           ("stock_info_sz_delist", "终止上市日期")):
            fn = getattr(ak, name, None)
            if not callable(fn):
                continue
            try:
                d = fn()
            except Exception as exc:  # noqa: BLE001
                print(f"  {name}: 失败 {type(exc).__name__}: {str(exc)[:100]}")
                continue
            print(f"\n  {name}: {len(d)} 行")
            dc = dcol if dcol in d.columns else next(
                (c for c in d.columns if "终止" in str(c) or "暂停" in str(c)), None)
            if dc:
                s = pd.to_datetime(d[dc], errors="coerce")
                vc = s.dt.year.value_counts().sort_index()
                print(f"    按【{dc}】统计：")
                print(vc.to_string())
                print(f"    合计 {int(vc.sum())} 只；"
                      f"2018 年后 = {int(vc[vc.index >= 2018].sum())} 只")
    except Exception as exc:  # noqa: BLE001
        print(f"  akshare 不可用：{exc}")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
