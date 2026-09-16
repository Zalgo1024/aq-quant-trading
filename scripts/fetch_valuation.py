"""P2.5：全市场历史估值 + 股本拉取脚本。

数据源：akshare ``stock_value_em``（东方财富个股估值历史）

用法::

    python scripts/fetch_valuation.py --limit 20        # 小样本验证
    python scripts/fetch_valuation.py                   # 全市场（约 12 分钟）
    python scripts/fetch_valuation.py --workers 1       # 限流时退化为串行
    python scripts/fetch_valuation.py --symbols 000001,600000
    python scripts/fetch_valuation.py --force           # 已存在也重拉

产出：``data_cache/valuation/{symbol}.parquet``，列名为英文：

============  ============  ====================================
列            单位          说明
============  ============  ====================================
date          -             交易日
close         元            当日收盘价（不复权，与 bars 的 close_raw 同源）
chg           -             当日涨跌幅
total_mv      元            总市值
float_mv      元            流通市值
total_share   股            总股本
float_share   股            流通股本
pe_ttm        -             市盈率 TTM
pe_static     -             市盈率 静态
pb            -             市净率
peg           -             PEG
pcf           -             市现率
ps            -             市销率
============  ============  ====================================

实测（2026-09-17，12 只跨板块抽样）：

- 成功率 **11/12**（仅北交所 ``430047`` 返回 None → 判为 nodata，非错误）
- 速度 **0.54 s/只** → 全市场 5256 只，4 线程约 **12 分钟**
- 覆盖 **2018-01-02 ~ 最新**，与 ``data_cache/bars`` 起点一致
- 老股 2114 行；新股按上市日起（如中芯国际 688981 自 2020-07-16）

⚠️ 前视偏差（重要）：

``pe_ttm`` / ``pb`` 的分母（净利润、净资产）来自财报，而财报有披露滞后。
抽查 000001 时发现 ``pe_ttm`` 与 ``pb`` 的日变动**逐位相等**，说明区间内
分母未更新、只是股价在动 —— 该源大概率是"当时快照"，但样本不足以下定论。

因此**本脚本只负责原样落盘，不做任何滞后处理**；滞后在因子层统一做
（见 ``aq/factors/vlib.py`` 的 ``ep_lag90`` / ``bp_lag60``）。
把"数据"与"口径修正"分开，是为了让修正可复查、可回滚。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aq.config.settings import load_settings  # noqa: E402
from aq.data.store import BarStore  # noqa: E402

_print_lock = threading.Lock()

# akshare 原始中文列名 -> 英文列名（顺序敏感，按探测输出的列序）
COLMAP = [
    ("数据日期", "date"),
    ("当日收盘价", "close"),
    ("当日涨跌幅", "chg"),
    ("总市值", "total_mv"),
    ("流通市值", "float_mv"),
    ("总股本", "total_share"),
    ("流通股本", "float_share"),
    ("PE(TTM)", "pe_ttm"),
    ("PE(静)", "pe_static"),
    ("市净率", "pb"),
    ("PEG值", "peg"),
    ("市现率", "pcf"),
    ("市销率", "ps"),
]

OUT_DIRNAME = "valuation"


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """把 akshare 的中文列重命名成英文，并做最小校验。"""
    rename = {zh: en for zh, en in COLMAP if zh in df.columns}
    missing = [zh for zh, _ in COLMAP if zh not in df.columns]
    if missing:
        raise ValueError(f"缺少预期列：{missing}；实际列：{list(df.columns)}")

    out = df.rename(columns=rename)[[en for _, en in COLMAP]].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"]).sort_values("date")
    for c in out.columns:
        if c != "date":
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="拉取 A 股全市场历史估值 + 股本（东财源）")
    ap.add_argument("--limit", type=int, default=0, help="限制股票数量（0=全部）")
    ap.add_argument("--symbols", default="", help="指定股票代码，逗号分隔")
    ap.add_argument("--workers", type=int, default=4, help="并发线程数（1=串行）")
    ap.add_argument("--force", action="store_true", help="已存在也重新拉取")
    ap.add_argument("--offset", type=int, default=0, help="跳过前 N 只（断点用）")
    ap.add_argument("--sleep", type=float, default=0.0, help="每只之后的间隔秒数（限流时调大）")
    args = ap.parse_args()

    cfg = load_settings()

    try:
        import akshare as ak
    except ImportError as exc:
        print(f"[错误] 依赖缺失：{exc}\n请先执行：pip install akshare")
        return 1

    store = BarStore()
    out_dir = store.root.parent / OUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 股票列表
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        names = {s: s for s in symbols}
    else:
        meta_path = store.root.parent / "stock_list.parquet"
        if not meta_path.exists():
            print(f"[错误] 缺少股票列表：{meta_path}\n请先运行 scripts/fetch_meta.py")
            return 1
        df = pd.read_parquet(meta_path)
        if args.offset:
            df = df.iloc[args.offset:]
        if args.limit:
            df = df.head(args.limit)
        symbols = df["symbol"].astype(str).tolist()
        names = dict(zip(df["symbol"].astype(str), df.get("name", df["symbol"].astype(str))))

    todo = [s for s in symbols if args.force or not (out_dir / f"{s}.parquet").exists()]
    skipped = len(symbols) - len(todo)

    print(f"数据源 eastmoney(stock_value_em) | 并发 {args.workers}")
    print(f"待拉 {len(todo)} 只（跳过已存在 {skipped} 只）| 输出 {out_dir}")
    print("-" * 64)

    ok = fail = nodata = 0
    failed: list[str] = []
    notdata: list[str] = []
    t0 = time.time()
    done = 0

    def work(sym: str) -> tuple[str, str, str]:
        """返回 (symbol, 状态, 备注)；状态 ∈ ok / nodata / fail"""
        for attempt in range(1, 4):
            try:
                raw = ak.stock_value_em(symbol=sym)
                if raw is None or len(raw) == 0:
                    return sym, "nodata", "空返回（多为北交所）"
                df = _normalize(raw)
                if df.empty:
                    return sym, "nodata", "空表"
                df.to_parquet(out_dir / f"{sym}.parquet", index=False)
                return sym, "ok", f"{len(df)} 行 {df['date'].min().date()}~{df['date'].max().date()}"
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    return sym, "fail", f"{type(exc).__name__}: {str(exc)[:70]}"
                time.sleep(1.0 * attempt)
        return sym, "fail", "unknown"

    if args.workers <= 1:
        results = []
        for s in todo:
            results.append(work(s))
            if args.sleep:
                time.sleep(args.sleep)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(work, todo))

    for sym, st, note in results:
        done += 1
        if st == "ok":
            ok += 1
        elif st == "nodata":
            nodata += 1
            notdata.append(sym)
        else:
            fail += 1
            failed.append(sym)
            _log(f"  [失败] {sym} {names.get(sym, '')}: {note}")
        if done % 500 == 0:
            _log(f"  ... 进度 {done}/{len(todo)}（成功 {ok} / 无数据 {nodata} / 失败 {fail}）")

    # ------------------------------------------------------------------ 报告
    el = time.time() - t0
    print("-" * 64)
    print(f"完成：成功 {ok} | 无数据 {nodata} | 失败 {fail} | 跳过 {skipped}")
    if todo:
        print(f"耗时 {el/60:.1f} 分钟（{el/len(todo):.2f}s/只）")
    print(f"数据目录：{out_dir}")

    root = store.root.parent
    if failed:
        p = root / "valuation_failed.txt"
        p.write_text("\n".join(failed), encoding="utf-8")
        print(f"失败清单：{p}（可用 --symbols 重跑）")
    if notdata:
        p = root / "valuation_nodata.txt"
        p.write_text("\n".join(notdata), encoding="utf-8")
        print(f"无数据清单：{p}（多为北交所/次新股，属预期）")

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
