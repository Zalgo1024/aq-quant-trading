# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 里的既有指数日线。
# 2026-09-18 起纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/index_bars/（见 README「快速开始」）。
# ---------------------------------------------------------------------------
"""风险预算探针：给定「组合最大回撤 ≤ X%」的约束，反解权益仓位上限。

动机：用户给定「本金 < 5 万 + 可接受最大回撤 ≤ 10%」。
A 股任何权益多头在熊市都会击穿 10%，所以必须把权益暴露当成一个**预算**来分配，
而不是选完标的再祈祷回撤别超。这个探针回答：

  权益仓位 w = ? 时，把 rf 现金打底后的**组合**最大回撤才 ≤ 10%

做法：组合日收益 r_p = w·r_index + (1−w)·rf_d（近似每日再平衡到目标仓位），
在真实日序列上直接算最大回撤 —— 不做解析近似。

⚠️ 口径警告：`data_cache/index_bars/` 存的是**价格指数**（不含股息）。
   所以下表的"年化"低估了分红能力：红利类策略真实年化要再加 3~5pp 的股息。
   **做仓位决策看最大回撤是够的**（回撤主要由价格波动决定），
   但**不要拿这里的年化去和存款/理财比** —— 那需要阶段 1 补的全收益指数。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

def _project_root() -> Path:
    """向上找到含 config/base.yaml 的目录作为项目根。

    这样脚本放在 runtime/ 还是 scripts/probes/ 都能跑对。
    （本探针初版就是漏了这一步，把 data_cache 找成了 scripts/data_cache。）
    """
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "config" / "base.yaml").exists():
            return cand
    return here.parents[1]


ROOT = _project_root()
IDX = ROOT / "data_cache" / "index_bars"
PPY = 242
RF = 0.02

NAMES = {
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "000016": "上证50",
}
START = "2019-01-01"
WEIGHTS = [0.20, 0.30, 0.40, 0.50, 0.60, 0.80, 1.00]


def load_index(code: str) -> pd.Series:
    p = IDX / f"{code}.SH.parquet"
    if not p.exists():
        raise FileNotFoundError(f"缺 {p}（先跑 scripts/fetch_index_bars.py）")
    df = pd.read_parquet(p)
    cols = {c.lower(): c for c in df.columns}
    dc = next((cols[k] for k in ("date", "trade_date", "dt") if k in cols), df.columns[0])
    df = df.set_index(pd.to_datetime(df[dc]))
    cc = next((cols[k] for k in ("close", "close_price", "收盘") if k in cols), None)
    if cc is None:
        cc = df.select_dtypes("number").columns[0]
    s = pd.to_numeric(df[cc], errors="coerce").sort_index().pct_change()
    return s.dropna()


def max_dd(daily: pd.Series) -> float:
    nav = (1.0 + daily).cumprod()
    return float((nav / nav.cummax() - 1.0).min())


def ann_ret(daily: pd.Series) -> float:
    return float((1.0 + daily).prod() ** (PPY / len(daily)) - 1.0)


def ann_vol(daily: pd.Series) -> float:
    return float(daily.std(ddof=1) * np.sqrt(PPY))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    codes = args or list(NAMES)
    rf_d = RF / PPY
    series = {}
    for c in codes:
        try:
            s = load_index(c)
        except Exception as exc:  # noqa: BLE001
            print(f"  [缺] {c}: {exc}")
            continue
        s = s[s.index >= pd.Timestamp(START)]
        if len(s) > 100:
            series[c] = s

    if not series:
        print("没有可用指数。")
        return 1

    print("=" * 96)
    print(f"① 单买各指数（价格指数，不含股息）  {START} ~ 最后一天")
    print("=" * 96)
    print(f"  {'指数':<12}{'天数':>7}{'年化':>10}{'年化波动':>11}{'最大回撤':>11}{'卡玛':>9}")
    for c, s in series.items():
        ar, av, md = ann_ret(s), ann_vol(s), max_dd(s)
        calmar = ar / abs(md) if md else float("nan")
        print(f"  {NAMES.get(c, c):<12}{len(s):>7}{ar*100:>9.2f}%{av*100:>10.2f}%"
              f"{md*100:>10.2f}%{calmar:>9.3f}")

    print("\n" + "=" * 96)
    print("② 加现金打底后：组合 = w·指数 + (1−w)·rf(2%/年)，看最大回撤落在哪")
    print("=" * 96)
    print(f"  {'指数':<12}" + "".join(f"{int(w*100):>7}%" for w in WEIGHTS))
    for c, s in series.items():
        row = f"  {NAMES.get(c, c):<12}"
        for w in WEIGHTS:
            blended = w * s + (1.0 - w) * rf_d
            row += f"{max_dd(blended)*100:>7.1f}%"
        print(row)

    print("\n  反解：让最大回撤 ≤ 目标值的**最大**权益仓位")
    print("  （注意 max_dd 返回的是负数，须与「负的预算」比较 —— 初版在这里比反了）")
    for target in (0.08, 0.10, 0.12, 0.15, 0.20):
        row = f"    回撤预算 ≤{target*100:>4.0f}%  →  "
        parts = []
        for c, s in series.items():
            best = None
            for w in np.arange(0.0, 1.001, 0.05):
                if abs(max_dd(w * s + (1.0 - w) * rf_d)) <= target:
                    best = w            # 单调递增，最后一个满足的就是上限
                else:
                    break
            parts.append(f"{NAMES.get(c, c)} ≤{best*100:>3.0f}%" if best is not None
                         else f"{NAMES.get(c, c)} 空仓也不行")
        print(row + " | ".join(parts))

    print("\n" + "=" * 96)
    print("③ 水下区间清单（价格指数口径；回撤是什么时候发生的，比最大值本身更重要）")
    print("=" * 96)
    for c, s in series.items():
        nav = (1 + s).cumprod()
        dd = nav / nav.cummax() - 1.0
        episodes = []
        i, n = 0, len(dd)
        while i < n:
            if dd.iloc[i] >= -1e-12:
                i += 1
                continue
            peak_i = i - 1                       # 进入水下的前一日 = 峰值日
            j = i
            while j < n and dd.iloc[j] < -1e-12:
                j += 1
            seg = dd.iloc[i:j]
            trough_i = dd.index.get_loc(seg.idxmin())
            rec = dd.index[j] if j < n else None
            episodes.append((dd.index[peak_i], dd.index[trough_i], float(seg.min()),
                             rec, (dd.index[trough_i] - dd.index[peak_i]).days))
            i = j
        episodes.sort(key=lambda e: e[2])
        print(f"\n  --- {NAMES.get(c, c)} ---")
        for peak, trough, depth, rec, days in episodes[:4]:
            rec_s = f"恢复到 {rec.date()}（耗时 {(rec - trough).days} 天）" if rec is not None \
                    else "**至今未恢复**"
            print(f"    峰 {peak.date()} → 谷 {trough.date()}（{days} 天）"
                  f"  最深 {depth*100:6.2f}%   {rec_s}")
        if len(episodes) > 4:
            rest = episodes[4:]
            print(f"    其余 {len(rest)} 段较浅（最深 {rest[0][2]*100:.2f}%）")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
