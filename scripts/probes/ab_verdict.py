# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与 runtime/cscv/ 的既有结果。
# 2026-09-18 从 runtime/ 纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/（见 README「快速开始」第 5 步拉取全市场日线）。
# ---------------------------------------------------------------------------
"""B1 A/B 判定器：把两个 CSCV 结果按「验收三指标」并排比较。

设计原则（本项目反复踩坑得来的）
-------------------------------
1. **只认 CSCV 之后的 PBO / DSR / 最优 t（含超额口径）**，不认单次回测净值曲线。
   单看某个配置的夏普很漂亮是 `liquid_v2` 骗过我们的形状（个别配置漂亮、全样本不成立）。
2. PBO 的零假设基准**不是固定 0.5**，随候选集相关结构上移 → 必须连同 ρ̄ / N_eff 一起读。
3. 超额口径只在产物带 `excess_caliber` 标记时才可引用（否则是"双扣 rf"的旧值）。

用法::

    python runtime/ab_verdict.py --a liquid_neu_main --b liquid_cost1
    python runtime/ab_verdict.py --a liquid_neu_main --b liquid_cost1 --key wic_h20d_t0

输出：三指标对照 + 逐配置并排 + 判据结论。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
CSC_DIR = ROOT / "runtime" / "cscv"
EXPECTED_CALIBER = "raw_minus_benchmark_no_rf"


def _load(tag: str) -> dict:
    p = CSC_DIR / f"cscv_{tag}.json"
    if not p.exists():
        raise SystemExit(f"[错误] 找不到 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _metrics(d: dict) -> dict:
    v = d["verdict"]
    pe, de = d.get("pbo_excess"), d.get("dsr_excess")
    caliber = d.get("excess_caliber")
    out = {
        "pbo": d["pbo_main"]["pbo"],
        "pbo_S": d["pbo_main"]["n_subperiods"],
        "best": d["pbo_main"]["fullsample_best"],
        "t": v["t_stat"],
        "dsr": v["dsr"],
        "significant": v["significant"],
        "rho": d["config_correlation"]["mean_pairwise_corr"],
        "n_eff": d["config_correlation"]["n_eff"],
        "haircut": d["pbo_main"].get("haircut"),
    }
    if pe and de and caliber == EXPECTED_CALIBER:
        out.update({"pbo_ex": pe["pbo"], "ex_best": pe["fullsample_best"],
                    "ex_t": de["t_stat"], "ex_sr": de["sharpe_annual"],
                    "ex_dsr": de["deflated_sharpe"]})
    else:
        out.update({"pbo_ex": None, "ex_best": None, "ex_t": None,
                    "ex_sr": None, "ex_dsr": None, "caliber": caliber})
    return out


def _f(x, fmt="%.3f"):
    return "—" if x is None else (fmt % x)


def _row(label: str, a, b) -> str:
    return f"| {label:<26s} | {a:>18s} | {b:>18s} |"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="对照臂 tag（如 liquid_neu_main）")
    ap.add_argument("--b", required=True, help="实验臂 tag（如 liquid_cost1）")
    ap.add_argument("--key", default="", help="只比同后缀的配置，如 wic_h20d_t0")
    args = ap.parse_args()

    A, B = _load(args.a), _load(args.b)
    ma, mb = _metrics(A), _metrics(B)

    print("=" * 74)
    print(f"A（对照） = {args.a}")
    print(f"B（实验） = {args.b}")
    print("=" * 74)
    print()
    print(_row("验收指标", "A", "B"))
    print(_row("-" * 26, "-" * 18, "-" * 18))
    print(_row(f"PBO (S={ma['pbo_S']})", _f(ma["pbo"]), _f(mb["pbo"])))
    print(_row("最优配置 t（绝对）", _f(ma["t"]), _f(mb["t"])))
    print(_row("DSR", _f(ma["dsr"]), _f(mb["dsr"])))
    print(_row("全样本最优配置", str(ma["best"]), str(mb["best"])))
    print(_row("ρ̄ / N_eff", "%.3f / %.2f" % (ma["rho"], ma["n_eff"]),
               "%.3f / %.2f" % (mb["rho"], mb["n_eff"])))
    print(_row("haircut", _f(ma["haircut"]), _f(mb["haircut"])))
    print()
    print("超额口径（策略 − 基准，两边不扣 rf；需 excess_caliber 标记）")
    print(_row("PBO_excess", _f(ma["pbo_ex"]), _f(mb["pbo_ex"])))
    print(_row("  全样本最优", str(ma["ex_best"]), str(mb["ex_best"])))
    print(_row("超额年化夏普", _f(ma["ex_sr"]), _f(mb["ex_sr"])))
    print(_row("超额 t", _f(ma["ex_t"]), _f(mb["ex_t"])))
    print(_row("超额 DSR", _f(ma["ex_dsr"]), _f(mb["ex_dsr"])))
    for tag, m in ((args.a, ma), (args.b, mb)):
        if m["pbo_ex"] is None:
            print(f"  ⚠️ {tag} 的超额口径不可引用（caliber={m.get('caliber')!r}）")

    # 逐配置并排
    ca = {c["label"]: c for c in A["configs"]}
    cb = {c["label"]: c for c in B["configs"]}

    def _suffix(lbl: str) -> str:
        # 去掉 neu_/cp1_/f{set}_ 之类前缀，只留 w{w}_h{h}d_t{t}
        i = lbl.find("wic_")
        return lbl[i:] if i >= 0 else lbl

    sa = {_suffix(k): v for k, v in ca.items()}
    sb = {_suffix(k): v for k, v in cb.items()}
    keys = sorted(set(sa) & set(sb))
    if args.key:
        keys = [k for k in keys if args.key in k]
    print()
    print("逐配置并排（SR / alpha / IR）")
    print(_row("配置", "A", "B"))
    print(_row("-" * 26, "-" * 18, "-" * 18))
    for k in keys:
        x, y = sa[k], sb[k]
        av = "%+.3f / %+.4f / %+.3f" % (x["sharpe"], x["alpha"], x["information_ratio"])
        bv = "%+.3f / %+.4f / %+.3f" % (y["sharpe"], y["alpha"], y["information_ratio"])
        flag = "" if y["sharpe"] <= x["sharpe"] else " ↑"
        print(_row(k + flag, av, bv))

    # 判据结论
    print()
    print("=" * 74)
    print("判据（只认 CSCV 之后的三个指标）")
    print("=" * 74)
    win = 0
    if ma["t"] is not None and mb["t"] is not None:
        print(f"  最优 t      ：{ma['t']:+.3f} → {mb['t']:+.3f}"
              f"（{'改善' if mb['t'] > ma['t'] else '未改善'}；显著门槛 |t|≥2）")
    if ma["dsr"] is not None and mb["dsr"] is not None:
        print(f"  DSR         ：{ma['dsr']:.3f} → {mb['dsr']:.3f}"
              f"（{'改善' if mb['dsr'] > ma['dsr'] else '未改善'}；显著门槛 0.95）")
    if ma["pbo"] is not None and mb["pbo"] is not None:
        print(f"  PBO         ：{ma['pbo']:.3f} → {mb['pbo']:.3f}"
              f"（⚠️ 基准随候选集上移：ρ̄ {mb['rho']:.2f} / N_eff {mb['n_eff']:.2f}）")
    if ma["ex_t"] is not None and mb["ex_t"] is not None:
        print(f"  超额 t      ：{ma['ex_t']:+.3f} → {mb['ex_t']:+.3f}"
              f"（{'改善' if mb['ex_t'] > ma['ex_t'] else '未改善'}）")
        if mb["ex_t"] is not None and abs(mb["ex_t"]) >= 2:
            win += 1
    print()
    sig_b = bool(mb["significant"])
    if sig_b:
        print("  ✅ B 臂在**绝对口径**下达到显著（|t|≥2 且 DSR≥0.95）")
    elif mb["ex_t"] is not None and abs(mb["ex_t"]) >= 2:
        print("  ⚠️ B 臂**超额口径**显著但绝对口径不显著 —— 需人工判断是否可信（多重比较）")
    else:
        print("  ❌ 两个口径都不显著 → 按 §6 决策规则：**B1 未改善，现有框架封存**")
        print("     结论：IC 加权多因子在 A 股 2019-2026 这段样本上无可用 alpha。")
    print()
    print("  ⚠️ 不要用「个别配置 SR 变好」当证据 —— 那正是 liquid_v2 骗过我们的形状。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
