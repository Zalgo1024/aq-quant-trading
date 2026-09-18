#!/usr/bin/env python
"""拉取**逐日 ST 状态**（阶段 1b 收口：把 ST 过滤从「当前名称快照」改成「时变状态」）。

为什么必须做
------------
`aq/data/universe.py` 的 `exclude_st` 用 **当前** `name.contains("ST|退")` + `is_st` 快照，
而 ST 状态本应**时变**，于是产生**双向**错误：

1. 「当年正常、现在 ST」的股票 → 被**整段**剔除（连它当年正常经营的时段也丢了）；
2. 「当年 ST、现在摘帽」的股票 → 当年本该不入池，却被保留；
3. 退市股名称全带「退」+ 多数退市前长期 ST → 按快照口径 1b 的数据会被整体剔除，
   而正确的做法是**只在其 ST 期间**不入池。

实测（2026-09-18）：退市股 000004 在 5226 个交易日里有 **1802 天** isST=1（34%）。

数据源：baostock `query_history_k_data_plus(fields="date,isST")`（走自有 socket，不受代理影响）。

落盘：``data_cache/st_flags/{code}.parquet``（time, is_st），**不动 bars/ 任何文件**。
覆盖：现役（`stock_list.parquet`）∪ 退市（`delisted_list.parquet`）。

用法::

    python scripts/fetch_st_flags.py                    # 全量（串行，慢但安全）
    python scripts/fetch_st_flags.py --offset 0 --limit 1000   # 分段续传（**串行**）
    python scripts/fetch_st_flags.py --symbols 000001,000002   # 指定

⚠️⚠️ **严禁多进程并发**（2026-09-18 实踩）
----------------------------------------
baostock 对同一 IP 的反复 `login()` 有风控：跑 6 个并发进程几分钟后即被
**拉黑**（`login` 返回 `10001011 黑名单用户，请与管理员联系`），此后所有查询
**静默返回空**（不报错！）——表现为「大量 ➖ 无数据」，极易被误判为"该股没有数据"。
后果：`--limit 998` 的那一片里 89% 报空，且**封禁期间无法再登录**。
→ 本脚本只允许**串行**（或分段跑完再跑下一段），不要并行启动多个实例。
→ 熔断：连续 `MAX_CONSECUTIVE_EMPTY` 只返回空即判定为异常并中止（见下）。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from aq.config.settings import PROJECT_ROOT

_print_lock = threading.Lock()
_bs_lock = threading.Lock()

START = "2004-01-01"
END = "2026-12-31"

#: 连续多少只返回空就判定为「被拉黑/服务异常」并中止。
#: 依据：正常数据下单只股票极少无数据；被封禁时**查询静默返回空**（不报错），
#: 若不熔断会一路写完"空跑"日志而不自知（2026-09-18 实踩）。
MAX_CONSECUTIVE_EMPTY = 40


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def st_dir() -> Path:
    d = PROJECT_ROOT / "data_cache" / "st_flags"
    d.mkdir(parents=True, exist_ok=True)
    return d


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


def fetch_st(code: str, start: str, end: str) -> pd.DataFrame | None:
    """单只：返回 DataFrame(time, is_st)；无数据返回 None。"""
    bs = _bs()
    bs_code = ("sh." if code.startswith("6") else "sz.") + code
    rs = bs.query_history_k_data_plus(bs_code, "date,isST",
                                      start_date=start, end_date=end,
                                      frequency="d", adjustflag="3")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["time", "is_st"])
    df["time"] = pd.to_datetime(df["time"])
    df["is_st"] = (df["is_st"] == "1").astype("int8")
    return df


def all_codes() -> list[str]:
    """现役 ∪ 退市（裸代码，去重保序：现役在前）。"""
    out: list[str] = []
    seen = set()
    for p, col in ((PROJECT_ROOT / "data_cache" / "stock_list.parquet", "symbol"),
                   (PROJECT_ROOT / "data_cache" / "delisted_list.parquet", "code")):
        if not p.exists():
            continue
        for c in pd.read_parquet(p)[col].astype(str):
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="逐日 ST 状态（baostock）")
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbols", default="", help="逗号分隔的裸代码")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.symbols:
        codes = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        codes = all_codes()
        if args.offset:
            codes = codes[args.offset:]
        if args.limit:
            codes = codes[:args.limit]

    d = st_dir()
    todo = [c for c in codes if args.force or not (d / f"{c}.parquet").exists()]
    print(f"[ST 标记] 目标 {len(codes)} 只 | 已有 {len(codes) - len(todo)} | "
          f"待拉 {len(todo)} 只 | 目录 {d}")
    _bs()

    ok = fail = empty = n_st_total = 0
    streak_empty = 0
    fails: list[str] = []
    t0 = time.time()
    for i, c in enumerate(todo, 1):
        try:
            df = fetch_st(c, args.start, args.end)
            if df is None or df.empty:
                empty += 1
                streak_empty += 1
                _log(f"  ➖ {c} 无数据")
                if streak_empty >= MAX_CONSECUTIVE_EMPTY:
                    raise RuntimeError(
                        f"连续 {streak_empty} 只返回空 —— 极可能是被 baostock 拉黑"
                        f"（查询会静默返回空而不报错）。已中止；请等待解封后"
                        f"用 --offset/--limit 串行续传。")
                continue
            streak_empty = 0
            df.to_parquet(d / f"{c}.parquet", index=False)
            ok += 1
            n_st_total += int(df["is_st"].sum())
            if i % 200 == 0:
                _log(f"  …{i}/{len(todo)}（{time.time() - t0:.0f}s）")
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            fail += 1
            fails.append(c)
            _log(f"  ❌ {c} {type(exc).__name__}: {str(exc)[:60]}")
    el = time.time() - t0
    print("-" * 78)
    print(f"  ST 标记完成：成功 {ok} | 空 {empty} | 失败 {fail} | 用时 {el:.0f}s"
          f" | isST=1 总天数 {n_st_total}")
    if fails:
        p = PROJECT_ROOT / "data_cache" / "st_fetch_failed.txt"
        p.write_text("\n".join(fails), encoding="utf-8")
        print(f"  失败清单：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
