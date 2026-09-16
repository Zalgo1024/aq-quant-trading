"""一键因子研究（P2）。

用法::

    python scripts/factor_research.py                       # 默认：动态池 2022-01 起
    python scripts/factor_research.py --quick               # 小样本快速验证
    python scripts/factor_research.py --neutralize          # 行业+市值中性化后再算 IC
    python scripts/factor_research.py --universe hs300      # 换成静态指数池
    python scripts/factor_research.py --start 2023-01-01 --end 2025-12-31
    python scripts/factor_research.py --force               # 忽略面板缓存重建

产出写到 ``runtime/factor_research/<时间戳>/``：
* ``summary.csv``   每个因子的 IC / RankIC / ICIR / t / 多空 / 单调性
* ``ic_ts.parquet`` 逐日 IC 序列（前端画 IC 时序图用）
* ``corr.csv``      因子相关性矩阵
* ``report.md``     人读的报告
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aq.config.settings import PROJECT_ROOT  # noqa: E402
from aq.factors.ic import (  # noqa: E402
    factor_correlation, factor_report, prune_redundant, redundant_pairs,
)
from aq.factors.neutralize import neutralize  # noqa: E402
from aq.factors.panel import PanelConfig, FactorPanelBuilder  # noqa: E402
from aq.factors.vlib import spec_names  # noqa: E402

OUT_ROOT = PROJECT_ROOT / "runtime" / "factor_research"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="A 股因子研究（P2）")
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default="2026-09-15")
    p.add_argument("--universe", default="liquid",
                   help="liquid(动态流动性池,无前视) | all | hs300 | zz500 | zz800")
    p.add_argument("--factors", default="", help="逗号分隔，默认全部")
    p.add_argument("--neutralize", action="store_true", help="做行业+市值中性化后再检验")
    p.add_argument("--no-neutralize", dest="neutralize", action="store_false")
    p.add_argument("--min-amount", type=float, default=2e7, help="动态池日均成交额下限(元)")
    p.add_argument("--min-list-days", type=int, default=180)
    p.add_argument("--n-quantile", type=int, default=10)
    p.add_argument("--min-cross", type=int, default=50, help="每日最少样本数，否则跳过该日")
    p.add_argument("--max-symbols", type=int, default=0, help=">0 时只取前 N 只（调试）")
    p.add_argument("--quick", action="store_true", help="小样本快速跑（验证链路）")
    p.add_argument("--force", action="store_true", help="忽略面板缓存")
    p.add_argument("--counterpart", default="",
                   help="对照组结果目录（用于生成 icir_raw/icir_neu 对照列）")
    p.add_argument("--outdir", default="")
    p.set_defaults(neutralize=False)
    return p.parse_args(argv)


def _merge_counterpart(summary: pd.DataFrame, args, out_root: Path) -> pd.DataFrame:
    """把对照组（原始/中性化）的 ICIR 合并成 icir_raw / icir_neu 两列。

    有了这两列才能判断"某因子的预测力是否主要来自行业/市值暴露"——
    这是 IC 加权定权前的关键一步（否则会把风格暴露当 alpha 加权）。

    实现要点（踩过的坑）：
    - **不能依赖"先跑 raw 还是先跑 neu"**：旧实现只按 `out_root/full_neu`、
      `out_root/full_raw` 固定顺序探测，若当前跑的是 raw 而目录里恰好有两个
      `full_*`，就会把**自己**（同一侧）当对照组，合并出一个恒等于自己的列。
      现在改为先从 meta.json 读 `neutralized` 判断该目录属于哪一侧，只认
      **相反侧**，从根上排除自比对。
    - `--counterpart` 显式给定时**优先且独占**，不再被自动探测干扰。
    - 本函数对 `summary` 的列名形态不敏感（`因子` / `factor` 都能吃），
      返回的列名统一为 `factor` 形态，避免上游 rename 顺序影响结果。
    """
    if not {"factor", "rank_icir"} <= set(
        summary.rename(columns={"因子": "factor"}).columns
    ):
        return summary

    cur_neu = bool(getattr(args, "neutralize", False))
    mine, theirs = ("icir_neu", "icir_raw") if cur_neu else ("icir_raw", "icir_neu")

    def _dir_is_neutralized(d: Path) -> bool | None:
        """读 meta.json 判断该目录是中性化侧还是原始侧；读不到返回 None。"""
        mf = d / "meta.json"
        if not mf.exists():
            return None
        try:
            return bool(json.loads(mf.read_text(encoding="utf-8")).get("neutralized"))
        except Exception:  # noqa: BLE001
            return None

    # ---- 组装候选：显式 counterpart 独占，否则自动探测相反侧 ----
    manual = str(getattr(args, "counterpart", "") or "").strip()
    candidates: list[Path] = []
    if manual:
        candidates.append(Path(manual))
    else:
        for name in ("full_raw", "full_neu"):
            d = out_root / name
            flag = _dir_is_neutralized(d)
            # 只认与当前侧相反、且 meta 可判定的目录；meta 缺失时跳过，
            # 宁可没有对照列，也不要用同侧数据伪造出一列。
            if flag is None or flag == cur_neu:
                continue
            candidates.append(d)

    for c in candidates:
        f = c / "summary.csv"
        if not f.exists():
            continue
        try:
            other = pd.read_csv(f)
        except Exception:  # noqa: BLE001
            continue
        ncol = "因子" if "因子" in other.columns else "factor"
        if ncol not in other.columns:
            continue
        other[ncol] = other[ncol].astype(str).str.replace("_neu$", "", regex=True)
        other = other.set_index(ncol)
        oth = "rank_icir" if "rank_icir" in other.columns else "icir"
        if oth not in other.columns:
            continue

        base = summary.rename(columns={"因子": "factor"})
        if theirs in base.columns:
            return base
        # ---------------------------------------------------------------
        # **关键**：`rank_icir` 必须原样保留，不能 rename 成 icir_neu/icir_raw。
        #
        # 下游 `FactorScorer.set_ic_weights` 是按列名 `rank_icir` 找值的，
        # 一旦把它改名，IC 自动定权会静默抛 KeyError 并**降级为先验权重**，
        # 只在日志留一行 "[打分] IC 权重加载失败"——A/B 对照会因此得出
        # "IC 加权打不过先验权重"的**假结论**（两组其实跑的是同一套权重）。
        #
        # 正确做法：对照列是**附加信息**，不是替代品。这里另外算出本侧的
        # 自身列（icir_raw 或 icir_neu），与对侧列并存。
        # ---------------------------------------------------------------
        base[mine] = base["rank_icir"]
        base[theirs] = base["factor"].map(other[oth])
        n_hit = int(base[theirs].notna().sum())
        print(f"[对照] 已合并 {c.name}/summary.csv 的 ICIR -> {theirs}"
              f"（命中 {n_hit}/{len(base)} 个因子）；{mine} 由本侧 rank_icir 填充")
        if n_hit == 0:
            print("[对照][警告] 因子名一个都没对上，对照列全空——"
                  "请检查两侧 summary.csv 的因子命名是否一致")
        return base
    if not manual:
        print("[对照] 未找到相反侧结果目录（full_raw/full_neu），"
              "本次不生成 icir_raw/icir_neu 列")
    return summary.rename(columns={"因子": "factor"})


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    t0 = time.time()

    if args.quick:
        args.start = "2024-01-01"
        args.max_symbols = args.max_symbols or 300
        args.min_cross = 30

    factors = [f.strip() for f in args.factors.split(",") if f.strip()] or spec_names()

    cfg = PanelConfig(
        start=args.start,
        end=args.end,
        universe=args.universe,
        factors=factors,
        min_amount=args.min_amount,
        min_list_days=args.min_list_days,
        max_symbols=args.max_symbols or None,
    )
    panel = FactorPanelBuilder(cfg).build(force=args.force)
    if panel.empty:
        print("[错误] 面板为空")
        return 1

    print(f"\n[面板] {len(panel):,} 行 / {panel['symbol'].nunique()} 只 / "
          f"{panel['date'].nunique()} 个交易日 / {args.start}~{args.end}")

    # ---- 有效样本检查 ----
    fac_cols = [c for c in factors if c in panel.columns]
    coverage = panel[fac_cols].notna().mean().sort_values()
    dropped = coverage[coverage < 0.5].index.tolist()
    if dropped:
        print(f"[提示] 以下因子有效样本 <50%，自动剔除: {dropped}")
        fac_cols = [c for c in fac_cols if c not in dropped]

    print(f"[因子] 参检 {len(fac_cols)} 个")
    print(f"  有效样本覆盖率 最低5个: "
          + ", ".join(f"{k}={coverage[k]:.0%}" for k in coverage.index[:5]))

    # ---- 中性化 ----
    use_cols = list(fac_cols)
    if args.neutralize:
        print("\n[中性化] 行业 + 对数市值 ...")
        panel = neutralize(
            panel, fac_cols,
            date_col="date", industry_col="industry", mktcap_col="mktcap",
            suffix="_neu",
        )
        use_cols = [c + "_neu" for c in fac_cols]

    # ---- IC 检验 ----
    print("\n[IC 检验] ...")
    res = factor_report(
        panel,
        use_cols,
        date_col="date",
        n_q=args.n_quantile,
        min_cross=args.min_cross,
        tradable_col="t1_tradable",
    )
    summary = res["summary"].copy()
    # 中性化列改回原因子名，报告更好读
    if args.neutralize:
        summary["factor"] = summary["factor"].str.replace("_neu$", "", regex=True)

    # ---- 若同目录有对照组（原始<->中性化），合并出 icir_raw / icir_neu ----
    # 两者并存才能判断"某因子的预测力是否主要来自行业/市值暴露"，
    # 这是 IC 加权定权前的关键一步（否则会把风格暴露当 alpha 加权）。
    summary = _merge_counterpart(summary, args, OUT_ROOT)

    summary = summary.rename(columns={"factor": "因子"})
    cols_order = [
        "因子", "n_days", "rank_ic", "rank_icir", "rank_ic_t", "rank_ic_p",
        "ic", "icir", "pos_ratio", "q_ls", "q_mono", "turnover", "autocorr", "direction",
    ]
    # 对照列（icir_raw/icir_neu）紧跟 rank_icir，便于人工核对中性化是否提效
    for cc in ("icir_raw", "icir_neu"):
        if cc in summary.columns:
            cols_order.insert(cols_order.index("rank_icir") + 1, cc)
    decay_cols = [c for c in summary.columns if c.startswith("ic_h")]
    cols_order = [c for c in cols_order if c in summary.columns] + decay_cols
    # 兜底：任何既不在 cols_order 也不是衰减列的列（未来新增字段）
    # 一律追加到末尾，避免像 icir_raw/icir_neu 这样被白名单静默丢掉。
    _rest = [c for c in summary.columns if c not in cols_order]
    if _rest:
        cols_order += _rest

    print("\n" + "=" * 118)
    print("因子检验结果（按 |RankICIR| 降序）")
    print("=" * 118)
    view = summary[cols_order].copy()
    for c in view.columns:
        if view[c].dtype.kind == "f":
            view[c] = view[c].astype(float).round(4)
    print(view.to_string(index=False))

    # ---- 相关性 ----
    print("\n[相关性] ...")
    corr = factor_correlation(panel, use_cols, date_col="date", min_cross=args.min_cross)
    if args.neutralize:
        corr.index = [str(i).replace("_neu", "") for i in corr.index]
        corr.columns = [str(c).replace("_neu", "") for c in corr.columns]
    pairs = redundant_pairs(corr, 0.7)
    if pairs:
        print(f"高相关因子对（|ρ|>=0.7）共 {len(pairs)} 对，前 15：")
        for a, b, v in pairs[:15]:
            print(f"  {a:20s} ~ {b:20s}  ρ={v:+.3f}")
    else:
        print("无 |ρ|>=0.7 的冗余因子对")

    # ---- 去冗余：给出推荐因子集 ----
    keep, dropped = prune_redundant(summary.rename(columns={"因子": "factor"}), corr, threshold=0.85)
    print(f"\n[去冗余] ρ>=0.85 贪心剔除 {len(dropped)} 个，保留 {len(keep)} 个")
    for nm, because, v in dropped:
        print(f"  - {nm:20s} 因与 {because} 相关 ρ={v:+.3f} 被剔除")
    print("推荐因子集：" + ", ".join(keep))

    # ---- 验收 ----
    sig = summary[(summary["rank_ic_p"] < 0.05) & (summary["rank_ic"].abs() > 0.02)]
    strong = summary[(summary["rank_ic_p"] < 0.05) & (summary["rank_ic"].abs() > 0.03)]
    print("\n" + "=" * 118)
    print(f"验收：|RankIC|>0.02 且 p<0.05 的因子 {len(sig)} 个；"
          f"|RankIC|>0.03 且 p<0.05 的因子 {len(strong)} 个（目标 >=5）")
    if len(sig):
        print("入选：" + ", ".join(sig["因子"].tolist()))
    print("=" * 118)

    # ---- 落盘 ----
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = Path(args.outdir) if args.outdir else (OUT_ROOT / ts)
    outdir.mkdir(parents=True, exist_ok=True)

    # 按 cols_order 重排列序后落盘（含 icir_raw/icir_neu 对照列），
    # 与终端展示的列序保持一致，方便直接对照。
    #
    # 守卫：`rank_icir` 是下游 `set_ic_weights` 的取值列，丢了会让 IC 自动
    # 定权**静默降级为先验权重**（A/B 对照会得出假结论）。宁可在这里报错，
    # 也不要写出一个"看起来正常、实际用不了"的 summary.csv。
    if "rank_icir" not in summary.columns:
        raise RuntimeError(
            "summary 缺少 rank_icir 列——下游 IC 自动定权会降级为先验权重。"
            "请检查 _merge_counterpart 是否正确保留了该列（对照列应追加而非替换）。"
        )
    summary[cols_order].to_csv(outdir / "summary.csv", index=False, encoding="utf-8-sig")
    corr.to_csv(outdir / "corr.csv", encoding="utf-8-sig")
    res["ic_ts"].to_parquet(outdir / "ic_ts.parquet")
    pd.DataFrame(res["decay"]).to_csv(outdir / "decay.csv", encoding="utf-8-sig")

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start": args.start, "end": args.end, "universe": args.universe,
        "neutralized": args.neutralize, "n_rows": int(len(panel)),
        "n_symbols": int(panel["symbol"].nunique()),
        "n_days": int(panel["date"].nunique()),
        "n_factors": len(fac_cols),
        "factors": fac_cols,
        "n_significant": int(len(sig)),
        "n_strong": int(len(strong)),
        "recommended_factors": keep,
        "pruned_factors": [d[0] for d in dropped],
        "corr_threshold": 0.85,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (outdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / "recommended_factors.json").write_text(
        json.dumps({"factors": keep, "pruned": [{"factor": d[0], "corr_with": d[1], "rho": d[2]}
                                                for d in dropped]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_report(outdir / "report.md", meta, summary, corr, pairs, res, keep, dropped)

    print(f"\n[产出] {outdir}")
    for f in sorted(outdir.iterdir()):
        print(f"  - {f.name}  ({f.stat().st_size/1024:.1f} KB)")
    print(f"[耗时] {time.time()-t0:.1f}s")
    return 0


def _write_report(path: Path, meta: dict, summary: pd.DataFrame,
                  corr: pd.DataFrame, pairs, res: dict,
                  keep: list[str] | None = None,
                  dropped: list[tuple[str, str, float]] | None = None) -> None:
    lines = [
        "# 因子研究报告",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 样本区间：{meta['start']} ~ {meta['end']}",
        f"- 股票池：{meta['universe']}（{meta['n_symbols']} 只 / {meta['n_days']} 交易日 / {meta['n_rows']:,} 行）",
        f"- 中性化：{'行业 + 对数市值' if meta['neutralized'] else '未中性化'}",
        f"- 参检因子：{meta['n_factors']} 个",
        "",
        "## 结论摘要",
        "",
        f"- |RankIC| > 0.02 且 p < 0.05：**{meta['n_significant']}** 个",
        f"- |RankIC| > 0.03 且 p < 0.05：**{meta['n_strong']}** 个（P2 验收目标 >= 5）",
        "",
        "> RankIC 的 t 值已做 **Newey-West 修正**：持有期重叠会让 IC 序列自相关，",
        "> 普通 t 检验会系统性高估显著性。",
        "",
        "## 因子明细",
        "",
    ]
    show = summary.copy()
    num = show.select_dtypes("number").columns
    show[num] = show[num].astype(float).round(4)
    lines.append(show.to_markdown(index=False))
    lines += ["", "## 冗余因子对（|ρ| >= 0.7）", ""]
    if pairs:
        lines.append("| 因子A | 因子B | ρ |")
        lines.append("|---|---|---|")
        for a, b, v in pairs:
            lines.append(f"| {a} | {b} | {v:+.3f} |")
    else:
        lines.append("无")
    lines += ["", "## IC 衰减（持有期 RankIC 均值）", ""]
    dec = pd.DataFrame(res["decay"])
    lines.append(dec.round(4).to_markdown())
    lines += ["", "## 去冗余后的推荐因子集", ""]
    if keep:
        lines.append(f"相关阈值 ρ≥{meta.get('corr_threshold', 0.85)}，共保留 **{len(keep)}** 个：")
        lines.append("")
        lines.append("```")
        lines.append(", ".join(keep))
        lines.append("```")
        lines.append("")
        if dropped:
            lines.append("被剔除：")
            lines.append("")
            lines.append("| 因子 | 与谁高相关 | ρ |")
            lines.append("|---|---|---|")
            for nm, because, v in dropped:
                lines.append(f"| {nm} | {because} | {v:+.3f} |")
            lines.append("")
    else:
        lines.append("（未计算）")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
