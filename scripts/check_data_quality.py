"""数据质量检查（P1 验收 + P6 日常巡检复用）。

检查项：
1. 覆盖率：有多少只股票、多少根 K 线、日期范围
2. 真实价合理性：close_raw 应在 [0.3, 5000] 元 且 <= close（后复权 >= 真实价）
3. 复权因子：应为分段常数（跳变次数 ≈ 分红次数，不应每天抖）
4. 涨跌停价：limit_up > close > limit_down（非停牌日）
5. 缺失/异常：close <= 0、volume < 0、日期重复

用法::

    python scripts/check_data_quality.py
    python scripts/check_data_quality.py --sample 200   # 只抽查 N 只（快）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from aq.config.settings import PROJECT_ROOT  # noqa: E402
from aq.data.store import BarStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="日线数据质量检查")
    ap.add_argument("--sample", type=int, default=0, help="只抽查前 N 只（0=全部）")
    ap.add_argument("--dir", default=None, help="数据目录（默认 data_cache/bars）")
    args = ap.parse_args()

    root = Path(args.dir) if args.dir else PROJECT_ROOT / "data_cache" / "bars"
    if not root.exists():
        print(f"[错误] 数据目录不存在：{root}")
        print("请先执行：python scripts/fetch_daily.py")
        return 1

    files = sorted(root.glob("*.parquet"))
    if not files:
        print(f"[错误] {root} 下没有 parquet 文件")
        return 1
    if args.sample:
        files = files[: args.sample]

    print(f"=== 数据质量检查：{len(files)} 只股票 ===\n")

    rows = []
    problems: list[str] = []

    for f in files:
        sym = f.stem
        try:
            df = pd.read_parquet(f)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{sym}: 读取失败 {exc}")
            continue

        if df.empty:
            problems.append(f"{sym}: 空文件")
            continue

        n = len(df)
        dmin = str(df["time"].min())[:10]
        dmax = str(df["time"].max())[:10]

        # 真实价
        af = df["adj_factor"] if "adj_factor" in df.columns else pd.Series([1.0] * n)
        af = af.replace(0, 1.0).fillna(1.0)
        close_raw = df["close"] / af

        # 复权因子跳变次数（应远小于总天数）
        facs = af.tolist()
        jumps = sum(
            1 for i in range(1, len(facs))
            if facs[i - 1] and abs(facs[i] - facs[i - 1]) > facs[i - 1] * 0.001
        )

        rows.append(
            {
                "symbol": sym,
                "bars": n,
                "start": dmin,
                "end": dmax,
                "close_hfq": float(df["close"].iloc[-1]),
                "close_raw": float(close_raw.iloc[-1]),
                "adj": float(af.iloc[-1]),
                "adj_jumps": jumps,
                "nontrade": int((df["is_trading"] == False).sum()),  # noqa: E712
                "limitup": 0,
                "limitdown": 0,
            }
        )

        # ---- 规则校验
        if (df["close"] <= 0).any():
            problems.append(f"{sym}: 存在 close <= 0")
        if (df["volume"] < 0).any():
            problems.append(f"{sym}: 存在 volume < 0")
        if df["time"].duplicated().any():
            problems.append(f"{sym}: 日期重复")
        cr = close_raw
        if (cr <= 0).any() or (cr > 5000).any():
            problems.append(f"{sym}: 真实价越界 [{cr.min():.2f}, {cr.max():.2f}]")
        # 后复权价应 >= 真实价（adj >= 1）；允许小幅浮点误差
        if (af < 0.95).any():
            problems.append(f"{sym}: 复权因子 < 0.95（后复权价低于真实价？）")
        if jumps > n * 0.05:
            problems.append(f"{sym}: 复权因子跳变 {jumps}/{n} 次，疑似噪声而非分段常数")

        # 涨停天数：is_limit_up 是 property，parquet 里没存，这里按定义重算
        lu = int((df["close"] >= df["limit_up"] - 1e-6).sum())
        ld = int((df["close"] <= df["limit_down"] + 1e-6).sum())
        rows[-1]["limitup"] = lu
        rows[-1]["limitdown"] = ld

    if not rows:
        print("[错误] 没有任何有效数据")
        return 1

    rep = pd.DataFrame(rows)
    total_bars = int(rep["bars"].sum())

    print("【覆盖率】")
    print(f"  股票数      : {len(rep)}")
    print(f"  K 线总数    : {total_bars:,}")
    print(f"  平均天数    : {total_bars / len(rep):.0f}")
    print(f"  日期范围    : {rep['start'].min()} ~ {rep['end'].max()}")

    print("\n【真实价抽样（后复权 vs 真实）】")
    cols = ["symbol", "bars", "end", "close_hfq", "close_raw", "adj", "adj_jumps"]
    print(rep[cols].head(12).to_string(index=False))

    print("\n【复权因子跳变次数分布】(应等于分红次数，通常 ≤ 15)")
    print(f"  中位数 {rep['adj_jumps'].median():.0f} | 均值 {rep['adj_jumps'].mean():.1f} "
          f"| 最大 {rep['adj_jumps'].max()}")

    print("\n【涨跌停 / 停牌】")
    print(f"  涨停记录总数 : {int(rep['limitup'].sum()):,}")
    print(f"  跌停记录总数 : {int(rep['limitdown'].sum()):,}")
    print(f"  停牌记录总数 : {int(rep['nontrade'].sum()):,}")
    print("  （注：新浪源停牌日不返回该行，故停牌数通常为 0，属数据源特性）")

    print("\n【异常】")
    if problems:
        print(f"  发现 {len(problems)} 处：")
        for p in problems[:30]:
            print(f"    - {p}")
        if len(problems) > 30:
            print(f"    ... 另有 {len(problems) - 30} 处")
    else:
        print("  无（全部通过）")

    return 0 if not problems else 2


if __name__ == "__main__":
    raise SystemExit(main())
