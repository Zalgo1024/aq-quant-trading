"""P1：全市场日线拉取脚本。

用法::

    python scripts/fetch_daily.py                        # 默认 2018-01-01 至今
    python scripts/fetch_daily.py --limit 100            # 先小批量试跑
    python scripts/fetch_daily.py --start 2020-01-01
    python scripts/fetch_daily.py --symbols 600000,000001
    python scripts/fetch_daily.py --workers 1            # 退化为串行（限流时用）

特性：
- **并发拉取**：默认 4 线程。实测新浪源 4 并发 0.39s/只、成功率 100%，
  相比串行（~2.0s/只）快约 5 倍，全市场 5500+ 只约 40 分钟完成；
- **双序列**：同时拉后复权（算收益率/因子）与不复权（真实价，用于撮合与资金计算），
  合成 ``Bar.adj_factor``；
- 断点续传：已存在的 parquet 默认跳过（``--force`` 覆盖）；
- 失败重试 3 次，失败清单写入 ``data_cache/fetch_failed.txt``；
- 落盘至 ``data_cache/bars/{symbol}.parquet``，与 ``BarStore`` 兼容。

⚠️ 请遵守 akshare 及上游数据接口的使用条款，建议收盘后批量更新，不要高频爬取。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 允许直接 `python scripts/fetch_daily.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aq.config.settings import load_settings  # noqa: E402
from aq.data.store import BarStore  # noqa: E402

_print_lock = threading.Lock()

# 主源查不到时的降级源（新浪不覆盖 CDR 存托凭证等，腾讯可以）
FALLBACK_SOURCE = {"sina": "tx", "tx": "sina", "eastmoney": "sina"}


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="拉取 A 股全市场日线（后复权 + 真实价）")
    ap.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=0, help="限制股票数量（0=全部）")
    ap.add_argument("--symbols", default="", help="指定股票代码，逗号分隔")
    ap.add_argument("--sleep", type=float, default=0.15, help="每批之后的间隔秒数")
    ap.add_argument("--force", action="store_true", help="已存在也重新拉取")
    ap.add_argument("--source", default="sina", choices=["sina", "tx", "eastmoney"], help="数据源")
    ap.add_argument("--offset", type=int, default=0, help="跳过前 N 只（断点用）")
    ap.add_argument("--workers", type=int, default=4, help="并发线程数（1=串行）")
    ap.add_argument("--no-fallback", dest="fallback", action="store_false",
                    help="关闭源降级（默认新浪查不到会换腾讯再试）")
    ap.set_defaults(fallback=True)
    ap.add_argument("--refresh-list", action="store_true",
                    help="强制重新拉取股票列表（默认复用本地 stock_list.parquet）")
    args = ap.parse_args()

    cfg = load_settings()
    start = args.start or getattr(cfg.data, "start", "2018-01-01")
    end = args.end or getattr(cfg.data, "end", "2026-12-31")

    try:
        from aq.data.akshare_provider import AkshareProvider
    except ImportError as exc:
        print(f"[错误] 依赖缺失：{exc}\n请先执行：pip install akshare")
        return 1

    provider = AkshareProvider(cfg, source=args.source)
    store = BarStore()

    # ------------------------------------------------------------------ 股票列表
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        names = {s: s for s in symbols}
        stocks = [{"symbol": s, "name": s, "industry": "", "board": "",
                   "list_date": "", "is_st": False} for s in symbols]
    else:
        import pandas as pd

        meta_path = store.root.parent / "stock_list.parquet"

        # 默认复用本地已保存的列表，不重新联网请求。
        # 原因：akshare 的 stock_info_a_code_name() 内部会起 mini_racer（V8 JS 引擎），
        # 该引擎非线程安全且偶发崩溃（实测拉取过程中进程直接 coredump）。
        # 列表变动频率极低（每日至多几只新股），没必要每次冒险重新拉。
        if meta_path.exists() and not args.refresh_list:
            stocks = pd.read_parquet(meta_path).to_dict(orient="records")
            print(f"复用本地股票列表：{meta_path}（{len(stocks)} 只）")
            provider.set_st_map({s["symbol"]: bool(s.get("is_st")) for s in stocks})
        else:
            print("正在获取股票列表 ...")
            stocks = provider.get_stock_list()
            pd.DataFrame(stocks).to_parquet(meta_path, index=False)
            print(f"股票列表已保存：{meta_path}（{len(stocks)} 只）")

        if args.offset:
            stocks = stocks[args.offset:]
        if args.limit:
            stocks = stocks[: args.limit]
        symbols = [s["symbol"] for s in stocks]
        names = {s["symbol"]: s["name"] for s in stocks}

    # 断点续传
    todo = [s for s in symbols if args.force or not store.exists(s)]
    skipped = len(symbols) - len(todo)

    total = len(todo)
    print(f"数据源 {args.source} | 并发 {args.workers} | 待拉 {total} 只（跳过已存在 {skipped} 只）")
    print(f"区间 {start} ~ {end}")
    print("-" * 64)

    ok = fail = nodata = 0
    failed: list[str] = []
    t0 = time.time()

    # 每个线程一个 provider 实例（akshare 内部有 session 状态，不共享更安全）
    local = threading.local()

    def get_prov(source: str = None):  # type: ignore[no-untyped-def]
        source = source or args.source
        key = f"prov_{source}"
        p = getattr(local, key, None)
        if p is None:
            p = AkshareProvider(cfg, source=source)
            p._is_st_map = dict(provider._is_st_map)  # 复用已解析的 ST 标记
            setattr(local, key, p)
        return p

    def work(sym: str) -> tuple[str, str, str]:
        """返回 (symbol, 状态, 备注)；状态 ∈ ok / nodata / fail"""
        st, note = _try_fetch(sym, args.source)
        if st == "fail" and args.fallback:
            #  자동降级：新浪查不到的（如 CDR 存托凭证 689009）换腾讯再试
            st2, note2 = _try_fetch(sym, FALLBACK_SOURCE[args.source])
            if st2 == "ok":
                return sym, "ok", note2 + " (fallback)"
        return sym, st, note

    def _try_fetch(sym: str, source: str) -> tuple[str, str]:
        for attempt in range(1, 4):
            try:
                bars = get_prov(source).get_daily(sym, start, end)
                if bars:
                    store.save(sym, bars)
                    return "ok", str(len(bars))
                return "nodata", ""
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    return "fail", str(exc)[:80]
                time.sleep(1.5 * attempt)
        return "fail", "unknown"

    if args.workers <= 1:
        results = [work(s) for s in todo]
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(work, todo))

    for sym, st, note in results:
        if st == "ok":
            ok += 1
        elif st == "nodata":
            nodata += 1
        else:
            fail += 1
            failed.append(sym)
            _log(f"  {sym} {names.get(sym, '')} 失败: {note}")

    # ------------------------------------------------------------------ 报告
    el = time.time() - t0
    print("-" * 64)
    print(f"完成：成功 {ok} | 无数据 {nodata} | 失败 {fail} | 跳过 {skipped}")
    print(f"耗时 {el/60:.1f} 分钟（{el/max(len(todo),1):.2f}s/只）")
    print(f"数据目录：{store.root}")

    if failed:
        p = store.root.parent / "fetch_failed.txt"
        p.write_text("\n".join(failed), encoding="utf-8")
        print(f"失败清单：{p}（可用 --symbols 重跑）")

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
