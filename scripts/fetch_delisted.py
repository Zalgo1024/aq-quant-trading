#!/usr/bin/env python
"""拉取**已退市 A 股**的历史日线（阶段 1b：消幸存者偏差的数据底座）。

为什么必须做
------------
现役池 `bars/` 只有"今天还活着的股票"。已退市的（乐视网、暴风、*ST 银鸽……）
不在里面 → 任何在现役池上做的研究都带着**幸存者偏差**：
`probe_survivorship.py` 实测 2018 年后退市 259 只（年均 ~32 只，
占 ~4500 只池子的 0.7%~1.2%/年），bp Top20 每年被退市股拖累 1.3~4.4pp。

数据源裁决（2026-09-18，三源实测后定案）
----------------------------------------
- ✅ **baostock（本脚本采用）**：与主池（新浪 hfq）日收益率中位差 **1.24e-06**、
  q99 1.07e-05（000002，2112 重叠日）；事实锚 2024-09-27 万科 +9.95%（公开涨停）✓。
  对退市股覆盖完整：PT金田A 回溯 1991-07，末行即退市日。走自有 socket，不受代理影响。
- ❌ 腾讯 `stock_zh_a_hist_tx`：hfq 收益率**错误**——万科 2024-09 涨停潮单日
  +4.5~4.9% vs 真实 +10%（比例系数 ~0.5），与主池相关系数仅 0.959，
  价格比年化漂移 +12.7%。**一个事实锚否决，别再试**。
  另坑：`stock_zh_a_hist_tx` 的 adjust="" 不生效，永远返回 hfq。
- ❌ 东财 `stock_zh_a_hist`：与主池中位差 1e-3~3e-3（不达标），且被代理间歇拦截。
- 新浪 `stock_zh_a_daily`：对退市代码全部 JSONDecodeError → 不可用。

⭐ 口径警告（必读）
------------------
1. **落进独立目录 `data_cache/delisted_bars/`**，文件名 = **裸 6 位代码**
   （与主池 bars/ 命名一致）。混用前的收益一致性检验：``--crosscheck N``。
2. hfq 构造：`adj_factor = close_hfq / close_raw`（同日比，两趟查询），
   与主池约定「后复权价 = 真实价 × adj_factor」一致。
3. 停牌日（tradestatus≠1）**整行丢弃**，与主池（新浪无停牌行）对齐。
4. volume 单位 = 股（baostock 与新浪一致，无需换算）。
5. 退市日期：沪市清单只有「暂停上市日期」（真终止日更晚），深市有
   「终止上市日期」；实际采用列记在清单 `date_col_note`。

用法::

    python scripts/fetch_delisted.py --list                 # 只建退市清单
    python scripts/fetch_delisted.py --start 2005-01-01     # 拉日线
    python scripts/fetch_delisted.py --crosscheck 5         # 两源收益率一致性检验
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from aq.config.settings import PROJECT_ROOT
from aq.data.store import BarStore

_print_lock = threading.Lock()
_bs_lock = threading.Lock()

#: 沪市 A 股代码前缀（排除 B 股 900 / 现金选择权等）
SH_A_PREFIX = ("60", "68")
#: 深市 A 股代码前缀（排除 B 股 200）
SZ_A_PREFIX = ("00", "30")

#: 两源收益率一致性判据：重叠日 |r_baostock − r_sina| 超过它 → 不能混用
#: 容差依据：真口径一致时中位差 ~1e-6、q99 ~1e-5（实测）；真口径错 ≥1e-2。
RET_DIFF_Q99_TOL = 5e-3
RET_DIFF_MED_TOL = 1e-4


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def delisted_store() -> BarStore:
    return BarStore(PROJECT_ROOT / "data_cache" / "delisted_bars")


# ---------------------------------------------------------------------------
# baostock 适配层（进程内单连接 + 线程锁串行化）
# ---------------------------------------------------------------------------

def _bs():
    import baostock as bs
    if getattr(bs, "_aq_logged_in", False):
        return bs
    with _bs_lock:
        if not getattr(bs, "_aq_logged_in", False):
            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
            bs._aq_logged_in = True
    return bs


def _bs_code(sym: str) -> str:
    return ("sh." if sym.startswith("6") else "sz.") + sym


def _bs_query(sym: str, start: str, end: str, adjustflag: str) -> pd.DataFrame | None:
    bs = _bs()
    rs = bs.query_history_k_data_plus(
        _bs_code(sym), "date,open,high,low,close,volume,amount,tradestatus",
        start_date=start, end_date=end, frequency="d", adjustflag=adjustflag)
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                     "volume", "amount", "tradestatus"])
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna(subset=["close"])


def bs_get_daily(sym: str, start: str, end: str) -> list:
    """baostock → list[Bar]（后复权口径，与主池一致）。

    adj_factor = close_hfq / close_raw（同日比）；停牌行丢弃；
    volume=0 或 raw=0 的行（无法定 adj_factor）丢弃。
    """
    from aq.core.models import Bar

    raw = _bs_query(sym, start, end, "3")
    hfq = _bs_query(sym, start, end, "1")
    if raw is None or hfq is None or raw.empty or hfq.empty:
        return []
    raw = raw[raw["tradestatus"] == "1"].set_index("date")
    hfq = hfq[hfq["tradestatus"] == "1"].set_index("date")
    j = raw.join(hfq, how="inner", rsuffix="_hfq")
    j = j[(j["close"] > 0) & (j["close_hfq"] > 0)]
    if j.empty:
        return []
    j["adj_factor"] = j["close_hfq"] / j["close"]
    bars = [
        Bar(symbol=sym, time=idx.to_pydatetime(),
            open=float(r["open_hfq"]), high=float(r["high_hfq"]),
            low=float(r["low_hfq"]), close=float(r["close_hfq"]),
            volume=float(r["volume"]), amount=float(r["amount"]),
            adj_factor=float(r["adj_factor"]))
        for idx, r in j.iterrows()
    ]
    return bars


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------

def fetch_delisted_list(refresh: bool = False) -> pd.DataFrame:
    """沪 + 深退市清单 → ``data_cache/delisted_list.parquet``。"""
    out_path = PROJECT_ROOT / "data_cache" / "delisted_list.parquet"
    if out_path.exists() and not refresh:
        df = pd.read_parquet(out_path)
        print(f"复用本地退市清单：{out_path}（{len(df)} 行）")
        return df

    import akshare as ak

    # 列名实况（2026-09-18 实测）：沪 = 公司代码/公司简称/上市日期/**暂停上市日期**；
    # 深 = 证券代码/证券简称/上市日期/**终止上市日期**。
    # ⚠️ 两列名都不含字面「退市」二字；沪市只给暂停上市日（真终止日更晚）。
    frames: list[pd.DataFrame] = []
    for exch, fn in (
        ("sh", ak.stock_info_sh_delist),
        ("sz", ak.stock_info_sz_delist),
    ):
        raw = fn()
        if raw is None or len(raw) == 0:
            print(f"  ⚠️ {exch} 退市清单为空")
            continue
        cols = list(raw.columns)
        code = next((raw[c] for c in cols if "代码" in c), None)
        name = next((raw[c] for c in cols if "简称" in c or "名称" in c), None)
        date_col = next(
            (c for c in cols if "终止上市" in c and "日期" in c),
            next((c for c in cols if "退市" in c and "日期" in c), None))
        if date_col is None:
            date_col = next((c for c in cols if "暂停上市" in c), None)
        if code is None:
            print(f"  ⚠️ {exch} 清单无代码列，实际列 = {cols}")
            continue
        df = pd.DataFrame({
            "code": code.astype(str).str.zfill(6),
            "name": name.astype(str) if name is not None else "",
            "delist_date": pd.to_datetime(raw[date_col], errors="coerce") if date_col else pd.NaT,
            "date_col_note": date_col or "",
            "exchange": exch,
        })
        frames.append(df)
        print(f"  {exch} 退市清单 {len(df)} 行（日期列 =「{date_col}」，"
              f"非空 {int(df['delist_date'].notna().sum())} 行）")
    if not frames:
        raise RuntimeError("沪深两所退市清单都没拿到")
    out = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["code"])

    # 只留 A 股（排除 B 股与异常代码）
    keep = out["code"].str.startswith(SH_A_PREFIX) | out["code"].str.startswith(SZ_A_PREFIX)
    dropped = out[~keep]
    if len(dropped):
        print(f"  剔除非 A 股 {len(dropped)} 只：{' '.join(dropped['code'].head(10))} ...")
    out = out[keep].sort_values("delist_date").reset_index(drop=True)
    out["symbol"] = out.apply(
        lambda r: ("sh" if r["exchange"] == "sh" else "sz") + r["code"], axis=1)

    # 与现役池的交集（理论上应为 0；不为 0 说明清单口径有问题，必须报出来）
    live = {p.stem for p in (PROJECT_ROOT / "data_cache" / "bars").glob("*.parquet")}
    overlap = out[out["code"].isin(live) | out["symbol"].isin(live)]
    if len(overlap):
        print(f"  ❌ 退市清单与现役池交集 {len(overlap)} 只 —— **清单口径可疑**，先查再入库：")
        for _, r in overlap.head(10).iterrows():
            print(f"      {r['symbol']} {r['name']} delist={str(r['delist_date'])[:10]}")

    out.to_parquet(out_path, index=False)
    print(f"  退市清单落盘：{out_path}（{len(out)} 行）")
    if out["delist_date"].notna().sum():
        yr = out["delist_date"].dt.year
        print("  按退市年份分布（近 12 年）：")
        print(yr.value_counts().sort_index().tail(12).to_string())
    return out


# ---------------------------------------------------------------------------
# 日线
# ---------------------------------------------------------------------------

def fetch_bars(start: str, end: str, workers: int, force: bool,
               limit: int, offset: int) -> None:
    lst = fetch_delisted_list()
    # ⚠️ 落盘用**裸 6 位代码**（与主池 bars/ 命名一致，path_of 直接以传入串命名）；
    # 交易所前缀在清单的 exchange 列里，不进文件名。
    codes = lst["code"].tolist()
    if offset:
        codes = codes[offset:]
    if limit:
        codes = codes[:limit]

    store = delisted_store()
    todo = [s for s in codes if force or not store.exists(s)]
    print(f"[退市日线] 目标 {len(codes)} 只 | 已有 {len(codes) - len(todo)} | "
          f"待拉 {len(todo)} 只 | 目录 {store.root}")

    _bs()  # 登录一次

    def work(sym: str) -> tuple[str, str, str]:
        for attempt in range(1, 4):
            try:
                bars = bs_get_daily(sym, start, end)
                if not bars:
                    return sym, "empty", "0 行"
                store.save(sym, bars)
                return sym, "ok", f"{len(bars)} 行"
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    return sym, "fail", f"{type(exc).__name__}: {str(exc)[:70]}"
                time.sleep(1.5 * attempt)
        return sym, "fail", "unknown"

    ok = fail = empty = 0
    fails: list[str] = []
    t0 = time.time()
    if todo:
        if workers <= 1:
            results = [work(s) for s in todo]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(work, todo))
        for sym, st, note in results:
            if st == "ok":
                ok += 1
                _log(f"  ✅ {sym} {note}")
            elif st == "empty":
                empty += 1
                _log(f"  ➖ {sym} 无数据")
            else:
                fail += 1
                fails.append(sym)
                _log(f"  ❌ {sym} {note}")
    el = time.time() - t0
    print("-" * 78)
    print(f"  退市日线完成：成功 {ok} | 空 {empty} | 失败 {fail} | 用时 {el:.0f}s"
          f"（{el / max(len(todo), 1):.2f}s/只）")
    if fails:
        p = PROJECT_ROOT / "data_cache" / "delisted_fetch_failed.txt"
        p.write_text("\n".join(fails), encoding="utf-8")
        print(f"  失败清单：{p}")
    if ok:
        _coverage_report(store)


def _coverage_report(store: BarStore) -> None:
    """覆盖率报告：这些年退市股**缺多少**，直接决定偏差能消掉多少。"""
    lst = pd.read_parquet(PROJECT_ROOT / "data_cache" / "delisted_list.parquet")
    have: list[dict] = []
    for sym in lst["code"]:                    # 落盘命名 = 裸代码（与主池一致）
        p = store.path_of(sym)
        if not p.exists():
            continue
        df = pd.read_parquet(p, columns=["time"])
        have.append({"code": sym, "n": len(df),
                     "first": df["time"].min(), "last": df["time"].max()})
    if not have:
        print("  [覆盖率] 无落盘数据")
        return
    cov = pd.DataFrame(have)
    m = lst.merge(cov, on="code", how="left")
    got = m[m["n"].notna()]
    print(f"  [覆盖率] 清单 {len(m)} 只 | 拿到日线 {len(got)} 只"
          f"（{len(got) / len(m):.1%}）")
    if got["delist_date"].notna().sum():
        yr = got["delist_date"].dt.year
        print("  按退市年份的覆盖（近 12 年）：")
        print(yr.value_counts().sort_index().tail(12).to_string())
        miss = m[m["n"].isna() & m["delist_date"].notna()]
        if len(miss):
            print(f"  ⚠️ 未覆盖 {len(miss)} 只（按年份）：")
            print(miss["delist_date"].dt.year.value_counts().sort_index().tail(12).to_string())
    print("  >>> 判读：覆盖不全时，**必须**把缺口年份记进文档 ——")
    print("      '消了一半的幸存者偏差'与'消掉了'是两个不同的结论。")


# ---------------------------------------------------------------------------
# 两源一致性检验（混用前的必要条件）
# ---------------------------------------------------------------------------

def crosscheck(n: int) -> int:
    """baostock vs 主池（新浪源）的**日收益率**一致性。

    两边都是后复权口径；判据：重叠日 |Δr| 中位 < 1e-4 且 99 分位 < 5e-3。
    （实测 baostock vs 新浪中位差 ~1e-6；腾讯源 ~1e-3 且涨停日差 5pp，已否决。）
    """
    store = BarStore()                      # 主池（新浪源）
    live = sorted(p.stem for p in store.root.glob("*.parquet"))
    pool = live[:n] if n < len(live) else live
    print(f"[交叉检验] 抽 {len(pool)} 只现役股：baostock vs 主池（新浪源）日收益率")
    print(f"  {'代码':<10}{'重叠天数':>9}{'中位|Δr|':>13}{'99分位|Δr|':>13}  判定")
    worst_med = worst_q99 = 0.0
    for sym in pool:
        try:
            bars = bs_get_daily(sym, "2018-01-01", "2026-09-15")
            if not bars:
                print(f"  {sym:<10} baostock 无数据，跳过")
                continue
            tx = pd.DataFrame([b.model_dump() for b in bars])
            tx = tx.set_index(pd.to_datetime(tx["time"]))["close"].sort_index()
            main = pd.read_parquet(store.path_of(sym), columns=["time", "close"])
            main = main.set_index(pd.to_datetime(main["time"]))["close"].sort_index()
            both = pd.DataFrame({"tx": tx, "main": main}).dropna()
            if len(both) < 30:
                print(f"  {sym:<10}{len(both):>9}  重叠不足，跳过")
                continue
            d = (both["tx"].pct_change() - both["main"].pct_change()).abs().dropna()
            med, q99 = float(d.median()), float(d.quantile(0.99))
            worst_med, worst_q99 = max(worst_med, med), max(worst_q99, q99)
            ok = med <= RET_DIFF_MED_TOL and q99 <= RET_DIFF_Q99_TOL
            print(f"  {sym:<10}{len(d):>9}{med:>13.2e}{q99:>13.2e}"
                  f"  {'OK' if ok else '**FAIL → 不可混用**'}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym:<10} 失败 {type(exc).__name__}: {str(exc)[:60]}")
    print(f"  >>> 最大 中位|Δr| = {worst_med:.2e}（< {RET_DIFF_MED_TOL:.0e}）、"
          f"99 分位 = {worst_q99:.2e}（< {RET_DIFF_Q99_TOL:.0e}）")
    if worst_med > RET_DIFF_MED_TOL or worst_q99 > RET_DIFF_Q99_TOL:
        print("  ❌ 两源收益率不一致 → 退市池**不能**与主池混用，先查复权口径。")
        return 1
    print("  ✅ 收益率层面可混用（两边均为后复权口径）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="退市股历史日线（阶段 1b，baostock 源）")
    ap.add_argument("--list", action="store_true", help="只建退市清单")
    ap.add_argument("--refresh-list", action="store_true", help="强制重拉清单")
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--workers", type=int, default=1,
                    help="baostock 单连接，默认串行；>1 时靠进程锁排队，提速有限")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--crosscheck", type=int, default=0,
                    help="抽 N 只现役股做 baostock vs 新浪收益率一致性检验")
    args = ap.parse_args()

    if args.crosscheck:
        return crosscheck(args.crosscheck)
    if args.list or args.refresh_list:      # ⚠️ --refresh-list 单独传也要只刷清单，别落进拉日线
        fetch_delisted_list(refresh=args.refresh_list)
        return 0
    fetch_bars(args.start, args.end, args.workers, args.force,
               args.limit, args.offset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
