"""每日增量更新：只拉最近 N 天并合并进本地 parquet（P1 收尾 / P6 日常用）。

全量拉取 5500+ 只要 35 分钟，显然不能每天跑一遍。本脚本：

1. 读取每只股票本地 parquet 的**最后一根 K 线日期**；
2. 从「最后日期 − overlap 天」开始拉（多拉几天是为了修正：除权、分红、
   数据源事后修正都会改写历史几天的复权价）；
3. 用新数据覆盖尾部，再与旧数据拼接去重；
4. 重新做一次**复权因子分段常数化**（增量段的因子要跟历史段对齐）。

用法::

    python scripts/update_daily.py                    # 更新已落盘的全部股票
    python scripts/update_daily.py --overlap 15       # 多覆盖 15 天
    python scripts/update_daily.py --symbols 600000,000001
    python scripts/update_daily.py --workers 4 --limit 200

建议：每个交易日 **19:00 之后**跑（新浪源收盘后约 1 小时才稳定）。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from aq.config.settings import load_settings  # noqa: E402
from aq.data.adj import smooth_adj_factor  # noqa: E402
from aq.data.store import BarStore  # noqa: E402

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="日线增量更新")
    ap.add_argument("--overlap", type=int, default=10,
                    help="回补天数：从最后日期往前多拉 N 天，用于修正历史复权价")
    ap.add_argument("--symbols", default="", help="指定股票，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--source", default="sina", choices=["sina", "tx", "eastmoney"])
    ap.add_argument("--max-age", type=int, default=0,
                    help="只更新最后日期距今超过 N 天的（0=全部）")
    args = ap.parse_args()

    cfg = load_settings()
    store = BarStore()
    today = date.today()

    try:
        from aq.data.akshare_provider import AkshareProvider
    except ImportError:
        print("[错误] 未安装 akshare")
        return 1

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = sorted(p.stem for p in store.root.glob("*.parquet"))
        if args.limit:
            symbols = symbols[: args.limit]

    if not symbols:
        print("[错误] 本地没有任何日线数据，请先跑 scripts/fetch_daily.py")
        return 1

    # 计算每只股票的起始日期
    plan: list[tuple[str, date]] = []
    skip_fresh = 0
    for s in symbols:
        p = store.path_of(s)
        try:
            df = pd.read_parquet(p, columns=["time"])
        except Exception:  # noqa: BLE001
            continue
        if df.empty:
            continue
        last = pd.to_datetime(df["time"]).max().date()
        if args.max_age and (today - last).days <= args.max_age:
            skip_fresh += 1
            continue
        plan.append((s, last - timedelta(days=args.overlap)))

    _log(f"待更新 {len(plan)} 只（跳过近期已更新 {skip_fresh} 只）| 并发 {args.workers}")
    if not plan:
        _log("全部已是最新，无需更新")
        return 0

    local = threading.local()

    def get_prov():  # type: ignore[no-untyped-def]
        p = getattr(local, "prov", None)
        if p is None:
            p = AkshareProvider(cfg, source=args.source)
            local.prov = p
        return p

    def work(item):  # type: ignore[no-untyped-def]
        sym, start = item
        for attempt in range(1, 4):
            try:
                bars = get_prov().get_daily(sym, start.isoformat(), today.isoformat())
                if not bars:
                    return sym, "nodata", 0

                new_df = pd.DataFrame([b.model_dump(mode="json") for b in bars])
                new_df["time"] = pd.to_datetime(new_df["time"])

                old_df = pd.read_parquet(store.path_of(sym))
                old_df["time"] = pd.to_datetime(old_df["time"])

                # 新数据覆盖尾部（含 overlap 区间），再与更早的拼接
                cutoff = new_df["time"].min()
                merged = pd.concat([old_df[old_df["time"] < cutoff], new_df],
                                   ignore_index=True)
                merged = (merged.drop_duplicates(subset=["time"], keep="last")
                                .sort_values("time")
                                .reset_index(drop=True))

                # 整段重新做复权因子分段常数化（增量段要与历史段对齐）
                if "adj_factor" in merged.columns:
                    old_af = merged["adj_factor"].fillna(1.0).replace(0, 1.0).tolist()
                    new_af = smooth_adj_factor(old_af)
                    ratio = pd.Series(new_af) / pd.Series(old_af)
                    merged["adj_factor"] = new_af
                    for col in ("limit_up", "limit_down"):
                        if col in merged.columns:
                            merged[col] = merged[col] * ratio

                merged.to_parquet(store.path_of(sym), index=False)
                return sym, "ok", len(new_df)
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    return sym, "fail", str(exc)[:80]
                time.sleep(1.5 * attempt)
        return sym, "fail", "unknown"

    t0 = time.time()
    if args.workers <= 1:
        results = [work(i) for i in plan]
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(work, plan))

    ok = sum(1 for _, st, _ in results if st == "ok")
    nodata = sum(1 for _, st, _ in results if st == "nodata")
    fail = [s for s, st, _ in results if st == "fail"]

    _log("-" * 64)
    _log(f"完成：更新 {ok} | 无新数据 {nodata} | 失败 {len(fail)} | "
         f"耗时 {time.time() - t0:.1f}s")
    if fail:
        out = store.root.parent / "update_failed.txt"
        out.write_text("\n".join(fail), encoding="utf-8")
        _log(f"失败清单：{out}")
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
