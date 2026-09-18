# -*- coding: utf-8 -*-
"""幸存者偏差修正（阶段 1b 验收探针）：把退市股并入面板后，结论怎么变。

与 `probe_survivorship.py` 的关系
--------------------------------
- 旧探针只读 `data_cache/bars`（现役池）：退市股**在 forward 观测点没有价 →
  被 `pd.notna(C.loc[d1, c])` 静默剔除**（这正是幸存者偏差本身）。
- 旧探针用 `px = close × adj_factor`，而 `close` 已是后复权价
  （`Bar` 约定：真实价 = close / adj_factor）→ **重复复权一次**
  （px = 真实价 × f²），高分红股收益被系统性高估。本探针给出两口径对照。

口径（全部显式标注）
------------------
- 价格：后复权 `close`（收益层面两源自洽，见 `docs/退市股数据层与源裁决.md`）。
- 池子：上市满 180 天 + 近 20 日日均成交额 ≥ 2e7。
- forward：活到观测日的用当日价；**未活到（退市/长期停牌）的用最后可得价**
  （退市可实现口径——A 股退市前有整理期，可卖出，不是 -100%）。
- 未处置偏差：**历史 ST 状态缺失**（主池按当前名称快照过滤，退市股全带「退」）。
  本探针**不做 ST 过滤**，与旧探针保持一致以便对照；该偏差单独登记。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _project_root() -> Path:
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "config" / "base.yaml").exists():
            return cand
    return here.parents[1]


ROOT = _project_root()
sys.path.insert(0, str(ROOT))

import pandas as pd

BARS = ROOT / "data_cache" / "bars"
DBARS = ROOT / "data_cache" / "delisted_bars"
LIST = ROOT / "data_cache" / "stock_list.parquet"
DLIST = ROOT / "data_cache" / "delisted_list.parquet"

MIN_AMOUNT = 2e7
NDEC = 10
MIN_LIST_DAYS = 180
AUDIT_SAMPLE = 500          # A 段口径审计的抽样只数（读 adj_factor 较慢）


def sec(t: str) -> None:
    print("\n" + "=" * 96)
    print(t)
    print("=" * 96)


def _read_one(p: Path, cols: list[str]) -> pd.DataFrame | None:
    try:
        df = pd.read_parquet(p, columns=cols)
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        return None
    df = df.set_index(pd.to_datetime(df["time"])).sort_index()
    if "is_trading" in df.columns:
        df = df[df["is_trading"] != False]  # noqa: E712
    return df if not df.empty else None


def load_all() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(后复权价宽表, 成交额宽表) —— 现役 ∪ 退市。"""
    closes, amounts = [], []
    for tag, d in (("现役", BARS), ("退市", DBARS)):
        files = sorted(d.glob("*.parquet"))
        print(f"  读取{tag} {len(files)} 只 …")
        for i, p in enumerate(files):
            df = _read_one(p, ["time", "close", "amount", "is_trading"])
            if df is None:
                continue
            closes.append(pd.to_numeric(df["close"], errors="coerce").rename(p.stem))
            amounts.append(pd.to_numeric(df["amount"], errors="coerce").rename(p.stem))
            if (i + 1) % 1000 == 0:
                print(f"    …{i + 1}/{len(files)}")
    return (pd.concat(closes, axis=1).sort_index(),
            pd.concat(amounts, axis=1).sort_index())


def year_ends(idx: pd.DatetimeIndex, years) -> dict[int, pd.Timestamp]:
    return {y: idx[idx.year == y][-1] for y in years if len(idx[idx.year == y])}


def build_pool(C: pd.DataFrame, A: pd.DataFrame, d0, list_date) -> list[str]:
    """上市满 180 天 + 近 20 日日均成交额 ≥ 2e7 + 起点有价。"""
    win = C.loc[:d0].tail(20)
    amt = A.loc[win.index].mean()
    ok = [c for c in amt[amt >= MIN_AMOUNT].index
          if c in list_date.index and pd.notna(list_date[c])
          and (d0 - list_date[c]).days >= MIN_LIST_DAYS
          and pd.notna(C.loc[d0, c])]
    return ok


def forward_table(C: pd.DataFrame, pool: list[str], d0, d1) -> pd.DataFrame:
    """treiling(12M) / forward(12M) / 是否活到 d1。"""
    pb = C.loc[:d0].iloc[-253] if C.loc[:d0].shape[0] > 253 else C.loc[:d0].iloc[0]
    p0 = C.loc[d0, pool]
    trail = p0 / pb.reindex(pool) - 1.0
    last_px = pd.Series({c: C[c].dropna().iloc[-1] for c in pool})
    alive = C.loc[d1, pool].notna()
    fwd = C.loc[d1, pool].fillna(last_px) / p0 - 1.0
    return pd.DataFrame({"trail": trail, "fwd": fwd, "alive": alive}).dropna(subset=["trail"])


def deciles(df: pd.DataFrame) -> pd.DataFrame:
    q = pd.qcut(df["trail"], NDEC, labels=False, duplicates="drop")
    g = df.groupby(q)
    return pd.DataFrame({"trail中位": g["trail"].median(),
                         "fwd中位": g["fwd"].median(),
                         "fwd均值": g["fwd"].mean(),
                         "n": g.size()})


def main() -> int:
    C, A = load_all()
    print(f"  面板 {C.shape[0]} 天 × {C.shape[1]} 只，"
          f"{C.index[0].date()} ~ {C.index[-1].date()}")

    sl = pd.read_parquet(LIST)
    dl = pd.read_parquet(DLIST)
    list_date = pd.concat([
        pd.to_datetime(sl.set_index("symbol")["list_date"], errors="coerce"),
        pd.to_datetime(dl.set_index("code")["list_date"], errors="coerce"),
    ])
    print(f"  元数据：现役 {len(sl)} + 退市 {len(dl)} 只")
    ends = year_ends(C.index, range(2018, 2027))

    # ---------------- A 段：口径审计（旧 vs 正确） ----------------
    sec("A 口径审计：px=close×adj_factor（旧探针） vs px=close（正确）")
    sample = [c for c in C.columns[:AUDIT_SAMPLE]]
    adj_map: dict[str, pd.Series] = {}
    for c in sample:
        for d in (BARS, DBARS):
            p = d / f"{c}.parquet"
            if p.exists():
                df = _read_one(p, ["time", "adj_factor", "is_trading"])
                if df is not None:
                    adj_map[c] = pd.to_numeric(df["adj_factor"], errors="coerce")
                break
    d0, d1 = ends.get(2023), ends.get(2025)
    pool = build_pool(C, A, d0, list_date)
    pool_s = [c for c in pool if c in adj_map]
    T = forward_table(C, pool_s, d0, d1)
    pb_idx = -253 if C.loc[:d0].shape[0] > 253 else 0
    dback = C.loc[:d0].index[pb_idx]
    p0 = C.loc[d0, pool_s]
    pb = C.loc[dback, pool_s]
    # adj_factor 是分段常数 → 在日频索引上 ffill 后取值（避免"该股当时还没上市"导致空切片）
    adf = pd.DataFrame({c: adj_map[c] for c in pool_s}).reindex(C.index).ffill()
    f0 = adf.loc[d0]
    fb = adf.loc[dback]
    trail_old = (p0 * f0) / (pb * fb) - 1.0
    # forward 也用旧口径算一遍：px(d1)/px(d0) → 会把 f 的跨期增长当收益
    p1_all = C.loc[d1, pool_s].fillna(
        pd.Series({c: C[c].dropna().iloc[-1] for c in pool_s}))
    f1 = adf.loc[d1]
    fwd_old = (p1_all * f1) / (p0 * f0) - 1.0
    audit = pd.DataFrame({"trail_old": trail_old, "trail_new": T["trail"],
                          "fwd": T["fwd"], "fwd_old": fwd_old}).dropna()
    for name, tcol, fcol in (("旧 px=close×adj", "trail_old", "fwd_old"),
                             ("正确 px=close", "trail_new", "fwd")):
        q = pd.qcut(audit[tcol], NDEC, labels=False, duplicates="drop")
        g = audit.groupby(q)
        d1f, d10f = g[fcol].median().iloc[0], g[fcol].median().iloc[-1]
        print(f"  [{name}] n={len(audit)} D1 fwd={d1f:+.2%} D10 fwd={d10f:+.2%} "
              f"D1−D10={(d1f - d10f) * 100:+.2f}pp")
    print("  >>> 上行 vs 下行 = 旧探针口径引入的偏差（f² 跨期增长被当成收益）")

    # ---------------- B 段：并入退市股前后 ----------------
    sec("B 退市股并入 vs 不并入：动量十档 forward（正确口径）")
    rows, detail = [], []
    for y in range(2019, 2025):
        d0, d1 = ends.get(y - 1), ends.get(y + 1)
        if d0 is None or d1 is None:
            continue
        pool = build_pool(C, A, d0, list_date)
        if len(pool) < 200:
            continue
        T_all = forward_table(C, pool, d0, d1)
        T_old = T_all[T_all["alive"]]                     # 旧口径：只留活到 d1 的
        if len(T_old) < 200:
            continue
        sa, so = deciles(T_all), deciles(T_old)
        rows.append({"年": y, "n旧": len(T_old), "n新": len(T_all),
                     "D1旧": so["fwd中位"].iloc[0], "D10旧": so["fwd中位"].iloc[-1],
                     "D1新": sa["fwd中位"].iloc[0], "D10新": sa["fwd中位"].iloc[-1]})
        dead = T_all[~T_all["alive"]]
        if len(dead):
            qa = pd.qcut(T_all["trail"], NDEC, labels=False, duplicates="drop")
            dq = qa.reindex(dead.index).dropna()
            detail.append({"年": y, "池n": len(T_all), "退市n": len(dead),
                           "占比": len(dead) / len(T_all),
                           "fwd中位": float(dead["fwd"].median()),
                           "fwd最惨": float(dead["fwd"].min()),
                           "落D1比例": float((dq == 0).mean()),
                           "档位中位": float(dq.median())})

    R = pd.DataFrame(rows)
    print(R.to_string(index=False))
    print("\n  分组汇总（跨年中位）：")
    print("    不含退市股：D1 {:+.2%} | D10 {:+.2%} | D1−D10 {:+.2f}pp".format(
        R["D1旧"].median(), R["D10旧"].median(),
        (R["D1旧"] - R["D10旧"]).median() * 100))
    print("    含退市股  ：D1 {:+.2%} | D10 {:+.2%} | D1−D10 {:+.2f}pp".format(
        R["D1新"].median(), R["D10新"].median(),
        (R["D1新"] - R["D10新"]).median() * 100))
    print(f"    → D1 档修正量（含−不含，中位）= "
          f"{(R['D1新'] - R['D1旧']).median() * 100:+.2f}pp")

    # ---------------- C 段：退市股画像 ----------------
    sec("C 退市股画像")
    D = pd.DataFrame(detail)
    if len(D):
        print(D.to_string(index=False))
        print(f"\n  退市股 forward 中位 = {D['fwd中位'].median():+.1%}"
              f" | 最惨 = {D['fwd最惨'].min():+.1%}"
              f" | 落 D1 比例中位 = {D['落D1比例'].median():.1%}"
              f" | 档位中位 = {D['档位中位'].median():.1f}（0 = D1 跌最多）")
    else:
        print("  本期无退市股落在池内")

    sec("D 未决缺口（如实登记）")
    print("  1) 历史 ST 状态缺失：主池用**当前名称快照**过滤 ST/退（`contains(\"ST|退\")`），")
    print("     退市股名称全带「退」→ 若按该规则过滤，1b 的数据几乎全被剔除；")
    print("     故本探针不做 ST 过滤。要真正落地需拉 baostock 逐日 isST。")
    print("  2) 估值类因子（bp）无法并入：退市股无估值数据 → 只能用动量口径量化拖累。")
    print("  3) 退市可实现损失用「最后可得价」——若退市后进入三板仍有残值，此处未计。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
