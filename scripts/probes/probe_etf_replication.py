# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与 runtime/cscv/ 的既有结果。
# 2026-09-18 从 runtime/ 纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/（见 README「快速开始」第 5 步拉取全市场日线）。
# ---------------------------------------------------------------------------
"""ETF 复制检验（只读产物，不改任何东西）。

上一步发现：策略的"alpha"里有一个极显著的规模暴露
（beta_size ≈ 0.41~0.48，t ≈ 13），剔除它之后 alpha 只剩 ~2.3%/年且 t < 0.6。

于是直接问：**能不能用两个 ETF + 现金把这个策略复制出来？**
若复制品的夏普/收益与原策略相当，那这个选股策略的全部价值 = 一个可被
ETF 免费复制的规模押注 —— 按 B3/B4 预注册的零假设「跑不赢直接买对应 ETF」即判负。

复制品构造（来自双因子回归 r_p = a + b_mkt·r300 + b_size·(r1000 − r300)）：
    r_p ≈ (b_mkt − b_size)·r300 + b_size·r1000 + (1 − b_mkt)·rf
即：沪深300 权重 (b_mkt − b_size)、中证1000 权重 b_size、其余放现金赚 rf。
"""
from __future__ import annotations

import math
import sys
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
CSC = ROOT / "runtime" / "cscv"
IDX = ROOT / "data_cache" / "index_bars"
PPY = 242
RF = 0.02
RF_D = RF / PPY

NAMES = {"000300": "沪深300", "000905": "中证500", "000852": "中证1000", "000016": "上证50"}


def load_index(code: str) -> pd.Series:
    df = pd.read_parquet(IDX / f"{code}.SH.parquet")
    cols = {c.lower(): c for c in df.columns}
    dc = next((cols[k] for k in ("date", "trade_date", "dt") if k in cols), df.columns[0])
    s = pd.to_numeric(df.set_index(pd.to_datetime(df[dc]))[
        next((cols[k] for k in ("close", "close_price", "收盘") if k in cols),
             df.select_dtypes("number").columns[0])], errors="coerce")
    return s.sort_index().pct_change()


def stats(d: pd.Series) -> tuple[float, float, float, float]:
    """(累计, 年化, 年化波动, 年化夏普)"""
    d = d.dropna()
    n = len(d)
    cum = float((1 + d).prod() - 1)
    ann = float((1 + cum) ** (PPY / n) - 1)
    vol = float(d.std(ddof=1) * math.sqrt(PPY))
    return cum, ann, vol, (ann / vol if vol else float("nan"))


def run(tag: str, dirname: str) -> None:
    rdir = CSC / dirname / "rets"
    if not rdir.exists():
        return
    import json
    js = json.loads((CSC / f"cscv_{dirname}.json").read_text(encoding="utf-8"))
    best = (js.get("pbo_main") or {}).get("fullsample_best")
    if not best:
        return

    r = pd.read_csv(rdir / f"{best}.csv", index_col=0, parse_dates=True).iloc[:, 0].dropna()
    i300 = load_index("000300")
    i1000 = load_index("000852")
    common = r.index.intersection(i300.dropna().index).intersection(i1000.dropna().index)
    y = r.loc[common].to_numpy()
    mkt = i300.loc[common].to_numpy()
    size = (i1000.loc[common] - i300.loc[common]).to_numpy()

    X = np.column_stack([np.ones(len(y)), mkt, size])
    beta = np.linalg.inv(X.T @ X) @ (X.T @ y)
    a, b_mkt, b_size = beta
    w300, w1000 = b_mkt - b_size, b_size
    w_cash = 1.0 - w300 - w1000

    print("=" * 96)
    print(f"[{tag}] {best}   ← 全样本最优配置")
    print(f"  回归：r_p = {a*PPY*100:.2f}%/年 + {b_mkt:.3f}·沪深300 "
          f"+ {b_size:.3f}·(中证1000−沪深300)")
    print(f"  ⇒ 隐含复制品：沪深300 {w300*100:.1f}% + 中证1000 {w1000*100:.1f}% "
          f"+ 现金 {w_cash*100:.1f}%（现金按 rf {RF*100:.0f}%/年 计息）")

    repl = w300 * mkt + w1000 * i1000.loc[common].to_numpy() + w_cash * RF_D
    repl = pd.Series(repl, index=common)
    pure = w300 * mkt + w1000 * i1000.loc[common].to_numpy()  # 不含 alpha 的纯复制

    rows = []
    for name, s in [("策略（选股）", r.loc[common]),
                    ("ETF 复制（+alpha）", repl),
                    ("ETF 复制（纯暴露，无 alpha）", pd.Series(pure, index=common)),
                    ("中证1000 单买", i1000.loc[common]),
                    ("沪深300 单买", i300.loc[common])]:
        cum, ann, vol, sr = stats(s)
        rows.append((name, cum, ann, vol, sr))
    print(f"\n  {'组合':<30}{'累计':>10}{'年化':>10}{'年化波动':>10}{'夏普':>9}")
    for name, cum, ann, vol, sr in rows:
        print(f"  {name:<30}{cum*100:>9.1f}%{ann*100:>9.2f}%{vol*100:>9.2f}%{sr:>9.3f}")

    s_strat = rows[0][4]
    s_repl = rows[1][4]
    s_1000 = rows[3][4]
    print(f"\n  判据（预注册：跑不赢 ETF 就别做）")
    print(f"    策略夏普 {s_strat:.3f}  vs  ETF 复制 {s_repl:.3f}   "
          f"差 {s_strat - s_repl:+.3f}  {'❌ 未跑赢' if s_strat <= s_repl else '✅ 跑赢'}")
    print(f"    策略夏普 {s_strat:.3f}  vs  单买中证1000 {s_1000:.3f}   "
          f"差 {s_strat - s_1000:+.3f}  {'❌ 未跑赢' if s_strat <= s_1000 else '✅ 跑赢'}")


def main() -> int:
    for t in (sys.argv[1:] or ["neu_main", "cost1"]):
        run(t, f"liquid_{t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
