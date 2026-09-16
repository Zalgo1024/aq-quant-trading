"""P1 遗留补全：抓取**指数日线行情**（基准对齐用）。

为什么单独一个脚本
------------------
``scripts/fetch_meta.py`` 只抓**指数成分股名单**（``index_constituents.parquet``），
**不抓指数本身的行情**。而回测要算真实的超额收益 / 信息比率 / alpha-beta，
必须有基准指数的日线序列。

为什么不放进 ``data_cache/bars/``
--------------------------------
A 股指数代码与深市个股代码**撞车**：

===========  ================  ==========================
代码          指数语义            bars/ 里同名的个股
===========  ================  ==========================
``000001``    上证指数           平安银行
``000016``    上证50              *ST康佳A
``000852``    中证1000           石化机械
``000905``    中证500            厦门港务
===========  ================  ==========================

所以指数行情落盘到**独立目录** ``data_cache/index_bars/``，文件名带市场
后缀（``000300.SH.parquet``），并打上 ``kind='index'`` 自证身份。读取走
``aq.data.index_store.IndexBarStore``。

数据源
------
``ak.stock_zh_index_daily_em(symbol="sh000300")`` —— 东财指数日线，
不需复权（指数无分红送转）。备选 ``ak.stock_zh_index_daily(symbol="sh000300")``
（新浪源）。两者列名不同，本脚本统一成
``time / open / high / low / close / volume / amount``。

用法::

    python scripts/fetch_index_bars.py                     # 抓默认 4 个宽基指数
    python scripts/fetch_index_bars.py --codes 000300 000905
    python scripts/fetch_index_bars.py --start 2018-01-01 --end 2026-09-15
    python scripts/fetch_index_bars.py --verify            # 只校验已落盘数据
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from aq.config.settings import PROJECT_ROOT  # noqa: E402
from aq.data.index_store import INDEX_UNIVERSE, IndexBarStore, resolve  # noqa: E402

CACHE = PROJECT_ROOT / "data_cache"

#: 默认抓取集合：覆盖基准（沪深300）与常用的风格/规模对照
DEFAULT_CODES = ["000300", "000905", "000016", "000852"]


def _norm_columns(df: pd.DataFrame) -> pd.DataFrame:
    """把不同数据源的列名统一成 time/open/high/low/close/volume/amount。"""
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    rename = {
        "日期": "time", "date": "time", "trade_date": "time",
        "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
        "成交量": "volume", "成交额": "amount",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    if "time" not in df.columns:
        raise ValueError(f"无法识别日期列，实际列：{list(df.columns)}")
    df["time"] = pd.to_datetime(df["time"])

    for c in ("open", "high", "low", "close"):
        if c not in df.columns:
            raise ValueError(f"缺列 {c}，实际列：{list(df.columns)}")
        df[c] = pd.to_numeric(df[c], errors="coerce")

    for c in ("volume", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = pd.NA  # 部分源不提供成交额

    keep = ["time", "open", "high", "low", "close", "volume", "amount"]
    return df[keep].dropna(subset=["close"])


def fetch_one(master: str, start: str | None, end: str | None) -> pd.DataFrame | None:
    """抓单个指数。``master`` 形如 ``000300.SH``。"""
    import akshare as ak

    meta = INDEX_UNIVERSE.get(master, {})
    ak_sym = meta.get("ak") or ("sh" + master.split(".")[0])
    label = f"{master}({meta.get('name', '?')})"

    # 1) 东财源（主）
    try:
        raw = ak.stock_zh_index_daily_em(symbol=ak_sym, start_date="19900101",
                                         end_date="20991231")
        df = _norm_columns(raw)
        print(f"  {label}: 东财源 {len(df)} 行")
    except Exception as exc:  # noqa: BLE001
        # 2) 新浪源（备）
        print(f"  {label}: 东财源失败（{type(exc).__name__}: {exc}），改试新浪源")
        try:
            raw = ak.stock_zh_index_daily(symbol=ak_sym)
            df = _norm_columns(raw)
            print(f"  {label}: 新浪源 {len(df)} 行")
        except Exception as exc2:  # noqa: BLE001
            print(f"  [失败] {label}: 两个源都不可用（{type(exc2).__name__}: {exc2}）")
            return None

    if start:
        df = df[df["time"] >= pd.Timestamp(start)]
    if end:
        df = df[df["time"] <= pd.Timestamp(end)]
    return df.reset_index(drop=True)


def fetch_all(codes: list[str], start: str | None, end: str | None) -> int:
    import akshare  # noqa: F401

    store = IndexBarStore()
    print(f"指数行情 → {store.root}")
    ok = 0
    for code in codes:
        master = resolve(code)
        df = fetch_one(master, start, end)
        if df is None or df.empty:
            print(f"  [跳过] {master}")
            continue
        p = store.save(master, df)
        print(f"  落盘 {master} {len(df)} 行 "
              f"（{df['time'].iloc[0].date()} ~ {df['time'].iloc[-1].date()}）→ {p.name}")
        ok += 1
        time.sleep(0.5)  # 轻微限速，避免被源站拒
    return ok


def verify() -> int:
    """校验已落盘指数：行数、区间、是否有个股污染。"""
    store = IndexBarStore()
    avail = store.available()
    if not avail:
        print("[空] index_bars/ 下没有任何指数数据")
        return 1

    try:
        cal = pd.read_parquet(CACHE / "calendar.parquet")
        cal_dates = pd.to_datetime(cal["trade_date"])
        # 覆盖率分母必须是**同区间**的交易日数，不能用全历史日历（8797 天，
        # 从 1990 年起）。踩过的坑：早期版本用了 len(cal)，把 2018 起的
        # 2113 行数据算成 24% 覆盖，误报「偏低」。
        cal_start, cal_end = cal_dates.min(), cal_dates.max()
    except Exception:  # noqa: BLE001
        cal_dates, cal_start, cal_end = None, None, None
        print("  [警告] 无 calendar.parquet，跳过覆盖率校验")

    print(f"已落盘 {len(avail)} 个指数：")
    bad = 0
    for name in avail:
        try:
            df = store.load(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  [异常] {name}: {exc}")
            bad += 1
            continue
        rng = f"{df['time'].iloc[0].date()} ~ {df['time'].iloc[-1].date()}"
        close0, close1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        note = ""
        if cal_dates is not None:
            # 用**数据自身区间**内的交易日数做分母
            lo, hi = df["time"].min(), df["time"].max()
            expected = int(((cal_dates >= lo) & (cal_dates <= hi)).sum())
            cov = len(df) / expected if expected else 0.0
            note = f" 覆盖 {cov:.1%}（{len(df)}/{expected}）"
            if cov < 0.98:
                note += "  ⚠️ 偏低"
        print(f"  {name}: {len(df):>5} 行 | {rng} | close {close0:.2f} → {close1:.2f}{note}")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取指数日线行情（基准对齐用）")
    ap.add_argument("--codes", nargs="*", default=DEFAULT_CODES,
                    help=f"要抓的指数，默认 {' '.join(DEFAULT_CODES)}")
    ap.add_argument("--start", default="2018-01-01", help="起始日期（含）")
    ap.add_argument("--end", default="2026-09-15", help="结束日期（含）")
    ap.add_argument("--verify", action="store_true", help="只校验已落盘数据，不抓取")
    args = ap.parse_args()

    print("=" * 64)
    if args.verify:
        print("校验模式")
        print("=" * 64)
        return verify()

    print(f"抓取指数日线 | {args.start} ~ {args.end}")
    print("=" * 64)
    try:
        import akshare  # noqa: F401
    except ImportError:
        print("[错误] 未安装 akshare，请先：pip install akshare")
        return 1

    ok = fetch_all(args.codes, args.start, args.end)
    print("-" * 64)
    print(f"完成：成功 {ok}/{len(args.codes)}")
    print("=" * 64)
    verify()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
