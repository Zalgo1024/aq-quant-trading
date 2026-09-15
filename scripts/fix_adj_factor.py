"""就地修复已落盘数据的 ``adj_factor``（无需重新联网）。

背景：``scripts/fetch_daily.py`` 早期版本直接用 ``close_hfq / close_raw`` 作为
复权因子，但新浪两个序列都只保留 2 位小数，低价股的"分"就是 0.2~0.4% 的
量化噪声 —— 质检发现 170/5218 只股票跳变了上百次（最多 1058 次）。

本脚本用 ``aq.data.adj.smooth_adj_factor`` 把因子还原成分段常数，并同步修正
``limit_up`` / ``limit_down``（它们在真实价尺度上是对的，只需按新因子换算回
后复权尺度）。

用法::

    python scripts/fix_adj_factor.py                # 全量
    python scripts/fix_adj_factor.py --sample 200   # 先试 200 只
    python scripts/fix_adj_factor.py --tol 0.01     # 自定义变点阈值
    python scripts/fix_adj_factor.py --dry-run      # 只看会改多少，不落盘
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from aq.config.settings import PROJECT_ROOT  # noqa: E402
from aq.data.adj import count_jumps, smooth_adj_factor  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="修复已落盘数据的复权因子")
    ap.add_argument("--dir", default=None, help="数据目录（默认 data_cache/bars）")
    ap.add_argument("--sample", type=int, default=0, help="只处理前 N 只")
    ap.add_argument("--tol", type=float, default=0.01, help="变点判定阈值（相对变化）")
    ap.add_argument("--dry-run", action="store_true", help="只报告不落盘")
    args = ap.parse_args()

    root = Path(args.dir) if args.dir else PROJECT_ROOT / "data_cache" / "bars"
    files = sorted(root.glob("*.parquet"))
    if args.sample:
        files = files[: args.sample]
    if not files:
        print(f"[错误] {root} 下没有 parquet")
        return 1

    print(f"=== 复权因子修复：{len(files)} 只（tol={args.tol}"
          f"{'，dry-run' if args.dry_run else ''}）===\n")

    changed = 0
    jump_before = jump_after = 0
    worst = []
    t0 = time.time()

    for f in files:
        try:
            df = pd.read_parquet(f)
        except Exception as exc:  # noqa: BLE001
            print(f"  {f.stem}: 读取失败 {exc}")
            continue
        if df.empty or "adj_factor" not in df.columns:
            continue

        old = df["adj_factor"].fillna(1.0).replace(0, 1.0).tolist()
        new = smooth_adj_factor(old, tol=args.tol)

        jb = count_jumps(old)
        ja = count_jumps(new)
        jump_before += jb
        jump_after += ja

        diff = sum(1 for a, b in zip(old, new) if abs(a - b) > 1e-9)
        if diff == 0:
            continue
        changed += 1
        worst.append((f.stem, jb, ja, diff))

        if not args.dry_run:
            ratio = pd.Series(new) / pd.Series(old)
            df["adj_factor"] = new
            for col in ("limit_up", "limit_down"):
                if col in df.columns:
                    df[col] = df[col] * ratio
            tmp = f.with_suffix(".parquet.tmp")
            df.to_parquet(tmp, index=False)
            try:
                import os
                os.replace(tmp, f)
            except OSError:
                import shutil
                shutil.copyfile(tmp, f)

    print("【跳变次数】")
    print(f"  修复前 {jump_before:,} | 修复后 {jump_after:,} "
          f"（减少 {jump_before - jump_after:,}，{(1 - jump_after / max(jump_before, 1)) * 100:.1f}%）")
    print(f"\n【受影响股票】{changed}/{len(files)} 只")

    worst.sort(key=lambda x: -(x[1] - x[2]))
    if worst:
        print("\n  改善最多的 10 只（代码 / 修复前跳变 / 修复后 / 改动行数）：")
        for sym, jb, ja, d in worst[:10]:
            print(f"    {sym}  {jb:>5} → {ja:>3}  （{d} 行）")

    print(f"\n耗时 {time.time() - t0:.1f}s"
          + ("（dry-run，未落盘）" if args.dry_run else f" | 目录 {root}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
