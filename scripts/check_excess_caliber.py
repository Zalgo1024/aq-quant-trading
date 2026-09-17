"""锁住「超额口径不得双扣 rf」这条不变量（2026-09-17 修复的 bug 的回归检查）。

背景
----
``scripts/cscv_test.py`` 的主矩阵 ``R`` 在扣无风险利率后会被**就地覆盖**
（``R = Rn``），而 ``--excess`` 那一段如果继续用 ``R`` 去减基准 ``B``，
算出来的其实是 ``策略 - rf - 基准`` —— 比"策略 - 基准"多扣了一份 rf（≈2%/年）。

这个 bug **不会报错**：它只会让 ``pbo_excess/dsr_excess`` 系统性偏低，
而代码自己的打印仍写着"策略日收益 − 基准日收益"。所以必须有一道
**独立实现**的比对把它按住。

本脚本的做法（与 cscv_test.py 走**不同**的代码路径）
--------------------------------------------------
对每个带 ``excess_caliber`` 标记的产物 json：
  1. 直接从 ``rets/<label>.csv``（原始日收益）与 ``rets/<label>.bench.csv`` 拼矩阵；
  2. 用 ``E = R_raw - B``（两边都不扣 rf）重算 PBO / DSR；
  3. 断言与 json 里存的 ``pbo_excess.pbo`` / ``dsr_excess`` 一致。

若 cscv_test.py 将来又改成用扣过 rf 的矩阵，第 3 步会失败 —— 这正是我们要的。

另：**没有** ``excess_caliber`` 字段的 json 一律记为"修正前的遗留产物"，
只统计不判定（它们是历史档案，README 第 5 节已标不可引用）。

用法
----
    python scripts/check_excess_caliber.py            # 检查全部
    python scripts/check_excess_caliber.py --tag neu_main
缺研究产物时优雅跳过（退出码 0），fresh clone 上也能跑。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from aq.backtest import cscv as C  # noqa: E402

CSC_DIR = PROJECT_ROOT / "runtime" / "cscv"
EXPECTED_CALIBER = "raw_minus_benchmark_no_rf"
TOL = 1e-6


def _load_matrix(rets_dir: Path) -> tuple[pd.DataFrame, int]:
    """从 rets 目录读原始日收益与基准收益，返回 (超额矩阵 E, 对齐天数)。

    ⚠️ 返回的是 **E = 原始策略收益 − 基准收益**（两边都不扣 rf）——
    本脚本要验证的就是这个式子，别把策略矩阵直接当 E 用。
    """
    rets: dict[str, pd.Series] = {}
    bench: dict[str, pd.Series] = {}
    for p in sorted(rets_dir.glob("*.csv")):
        name = p.name
        if name.endswith(".bench.csv") or name.endswith(".wf.csv"):
            continue
        label = name[:-4]
        r = pd.read_csv(p, parse_dates=["date"])
        rets[label] = pd.Series(r["ret"].values, index=[d.date() for d in r["date"]])
        bp = rets_dir / f"{label}.bench.csv"
        if bp.exists():
            rb = pd.read_csv(bp, parse_dates=["date"])
            bench[label] = pd.Series(rb["ret"].values, index=[d.date() for d in rb["date"]])
    R = pd.DataFrame(rets).dropna(how="any")
    B = pd.DataFrame(bench).dropna(how="any")
    if R.empty or B.empty:
        return pd.DataFrame(), 0
    common = R.index.intersection(B.index)
    E = R.loc[common] - B.loc[common]
    return E, len(common)


def _check_one(json_path: Path, verbose: bool) -> str:
    """返回 'pass' / 'fail' / 'legacy' / 'skip'。"""
    d = json.loads(json_path.read_text(encoding="utf-8"))
    caliber = d.get("excess_caliber")
    if not caliber:
        return "legacy"
    pe, de = d.get("pbo_excess"), d.get("dsr_excess")
    if not pe or not de:
        print(f"  [跳过] {json_path.name}：有 caliber 标记但没有 excess 结果")
        return "skip"
    if caliber != EXPECTED_CALIBER:
        print(f"  [失败] {json_path.name}：caliber 标记是 {caliber!r}，"
              f"期望 {EXPECTED_CALIBER!r}")
        return "fail"

    tag = json_path.stem
    # cscv_liquid_<tag> / cscv_hs300_<tag>：去掉前缀两段
    parts = tag.split("_")
    uni, sub = parts[1], "_".join(parts[2:])
    rets_dir = CSC_DIR / f"{uni}_{sub}" / "rets"
    if not rets_dir.exists():
        print(f"  [跳过] {json_path.name}：找不到 {rets_dir.relative_to(PROJECT_ROOT)}"
              f"（fresh clone 上没有 rets 缓存）")
        return "skip"

    E, n = _load_matrix(rets_dir)
    if E.empty:
        print(f"  [跳过] {json_path.name}：rets 缓存为空")
        return "skip"

    S = int(pe.get("n_subperiods") or 16)
    r = C.probability_of_backtest_overfitting(E, n_subperiods=S)
    if "error" in r:
        print(f"  [跳过] {json_path.name}：PBO 算不出（{r['error']}）")
        return "skip"
    bl = r["fullsample_best"]
    dsr = C.deflated_sharpe(E[bl], n_trials=len(E.columns))

    ok_pbo = abs(r["pbo"] - float(pe["pbo"])) < TOL
    ok_best = bl == pe.get("fullsample_best")
    ok_t = abs(dsr["t_stat"] - float(de["t_stat"])) < TOL
    ok_sh = abs(dsr["sharpe_annual"] - float(de["sharpe_annual"])) < TOL
    ok_dsr = abs(dsr["deflated_sharpe"] - float(de["deflated_sharpe"])) < TOL
    ok = ok_pbo and ok_best and ok_t and ok_sh and ok_dsr

    if verbose or not ok:
        print(f"  {'[通过]' if ok else '[失败]'} {json_path.name}")
        print(f"         对齐 {n} 日 / {E.shape[1]} 配置 / S={S} / 全样本最优 {bl}")
        print(f"         存量：PBO {pe['pbo']:.6f}  夏普 {de['sharpe_annual']:+.6f}"
              f"  t {de['t_stat']:+.6f}  DSR {de['deflated_sharpe']:.6f}")
        print(f"         重算：PBO {r['pbo']:.6f}  夏普 {dsr['sharpe_annual']:+.6f}"
              f"  t {dsr['t_stat']:+.6f}  DSR {dsr['deflated_sharpe']:.6f}")
        if not ok_best:
            print(f"         ⚠️ 全样本最优不一致：存量 {pe.get('fullsample_best')} vs 重算 {bl}")
    return "pass" if ok else "fail"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="", help="只查 cscv_*_<tag>.json")
    ap.add_argument("-v", "--verbose", action="store_true", help="每个结果都打印细节")
    args = ap.parse_args()

    if not CSC_DIR.exists():
        print(f"[跳过] 找不到 {CSC_DIR.relative_to(PROJECT_ROOT)}："
              f"先跑 CSCV（scripts/cscv_test.py）再执行本检查")
        return 0

    pat = f"cscv_*_{args.tag}.json" if args.tag else "cscv_*.json"
    files = sorted(CSC_DIR.glob(pat))
    if not files:
        print(f"[跳过] {CSC_DIR.relative_to(PROJECT_ROOT)} 下没有匹配 {pat} 的产物")
        return 0

    print("=" * 92)
    print("检查：超额口径不得重复扣无风险利率（E 必须 = 原始收益 − 基准）")
    print("=" * 92)
    counts = {"pass": 0, "fail": 0, "legacy": 0, "skip": 0}
    legacy = []
    for f in files:
        st = _check_one(f, args.verbose)
        counts[st] += 1
        if st == "legacy":
            legacy.append(f.name)

    print()
    print(f"结论：通过 {counts['pass']} / 失败 {counts['fail']} / "
          f"遗留（无 caliber 标记，只统计） {counts['legacy']} / 跳过 {counts['skip']}")
    if legacy:
        print("  遗留产物（README 第 5 节已标为修正前、不可引用）：")
        for nm in legacy:
            print(f"    - {nm}")
    if counts["fail"]:
        print()
        print("  ⚠️ 有产物与'原始收益 − 基准'不符 —— 说明超额口径又被改成用扣过 rf 的矩阵了，")
        print("     或基准序列对不齐。不要引用这些 pbo_excess / dsr_excess。")
        return 1
    if counts["pass"] == 0:
        print()
        print("  （没有可验证的产物：需要先跑 `cscv_test.py --excess` 生成带标记的结果）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
