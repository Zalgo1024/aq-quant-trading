"""因子打分器：横截面标准化 + 加权复合评分。

**复用彩票项目的思路**：综合评分 = Σ (因子归一化得分 × 权重)，
并输出每个因子的贡献明细，供前端做"因子归因/瀑布图"。

P2 升级
-------
1. 因子名对齐 :mod:`aq.factors.vlib`（旧名经 ``LEGACY_ALIAS`` 自动迁移）；
2. **IC 加权自动定权** —— 权重不再是拍脑袋的固定值，而是来自
   ``scripts/factor_research.py`` 实测的 RankICIR（带 Newey-West 修正的 t 值筛选）；
3. **方向自适应** —— 权重的符号直接由实测 IC 符号决定，先验方向只作兜底。

**重要不变量（前端瀑布图与冒烟测试都依赖）**：
``Σ contrib == score`` 且每个 ``contrib.value ∈ [0, 1]``。
为在"权重可正可负"的前提下守住它，采用定向得分::

    t_i  = tanh(z_i / 2)                  # ∈ [-1, 1]，方向已校正
    s_i  = 0.5 * (1 + sign(w_i) * t_i)    # ∈ [0, 1]
    contrib_i = |w_i| * s_i
    score     = Σ contrib_i = 0.5 * (1 + Σ w_i t_i) / Σ|w_i|

当所有权重为正时，这退化为 P0 版本的 ``w_i * (1+tanh(z_i/2))/2``，行为完全兼容。
"""

from __future__ import annotations

import math
from pathlib import Path
from statistics import mean, pstdev

from aq.core.models import FactorContrib, Prediction
from aq.factors.library import FactorLibrary, normalize_name

# 默认权重：IC 检验前的先验（组内均衡），会被 IC 加权覆盖
DEFAULT_WEIGHTS: dict[str, float] = {
    # 动量
    "mom_20": 0.045, "mom_60": 0.045, "mom_120": 0.030, "mom_60_vol_adj": 0.045,
    # 反转
    "rev_5": 0.060, "rev_20": 0.045,
    # 风险
    "vol_20": 0.040, "vol_60": 0.030, "dvol_20": 0.030,
    "mdd_20": 0.030, "skew_20": 0.025, "hl_range_20": 0.025,
    # 趋势
    "ma_bias_20": 0.035, "ma_bias_60": 0.030, "ma_cross_5_20": 0.040,
    "rsi_14": 0.030, "stoch_pos_20": 0.030,
    # 量能
    "vol_ratio": 0.035, "turnover_chg": 0.035, "amihud_20": 0.030,
    "amount_log_20": 0.040, "up_down_vol_20": 0.025, "vol_price_corr_20": 0.025,
    "turn_rate_20": 0.030,
    # 结构
    "days_since_high_60": 0.030, "limit_up_cnt_20": 0.030,
    "gap": 0.030, "intraday_ret": 0.030,
}


def _tofloat(v) -> float:
    """尽力转 float；None / 不可转换 -> NaN。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _latest_ic_summary() -> "Path | None":
    """runtime/factor_research 下最新一次研究的 summary.csv。"""
    root = Path(__file__).resolve().parents[2] / "runtime" / "factor_research"
    if not root.exists():
        return None
    cands = [d / "summary.csv" for d in root.iterdir() if d.is_dir()]
    cands = [p for p in cands if p.exists()]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def build_scorer(cfg=None, library: "FactorLibrary | None" = None) -> "FactorScorer":
    """按配置构造打分器（回测 / 模拟 / API 统一入口）。

    ``model.weight_source="ic"`` 时自动加载最近一次因子研究的 IC 结果定权；
    找不到结果时**静默降级**为先验权重，不会让线上流程崩掉。
    """
    lib = library or FactorLibrary()
    cfg = cfg
    if cfg is None:
        return FactorScorer(lib)

    m = getattr(cfg, "model", None)
    if m is None or getattr(m, "weight_source", "prior") != "ic":
        return FactorScorer(lib)

    path = getattr(m, "ic_summary_path", "") or ""
    p = Path(path) if path else _latest_ic_summary()
    if p is None or not Path(p).exists():
        print("[打分] 未找到 IC 检验结果，降级为内置先验权重"
              "（先跑 `python scripts/factor_research.py`）")
        return FactorScorer(lib)

    scorer = FactorScorer(lib)
    try:
        # 相关性矩阵：优先用与 summary 同目录的 corr.csv，启用相关性去冗余
        corr = Path(p).parent / "corr.csv"
        scorer.set_ic_weights(
            p,
            mode=getattr(m, "ic_weight_mode", "icir"),
            min_abs_ic=getattr(m, "ic_min_abs", 0.01),
            max_p=getattr(m, "ic_max_p", 0.10),
            cap=getattr(m, "ic_cap", 0.25),
            select=getattr(m, "ic_select", True),
            corr=corr if corr.exists() else None,
            corr_threshold=getattr(m, "ic_corr_threshold", 0.85),
        )
        print(f"[打分] 已加载 IC 权重 <- {Path(p).parent.name}"
              f"（{len(scorer.active_factors())} 个因子有权重，来源 {scorer.ic_source}）")
    except Exception as exc:  # noqa: BLE001
        # ---------------------------------------------------------------
        # **不可以静默降级。**
        #
        # 历史教训：summary.csv 的列名一旦不满足契约（例如 rank_icir 被
        # 误改名成 icir_neu），这里会吞掉 KeyError 并悄悄退回先验权重，
        # 只在日志留一行不起眼的提示。结果是 A/B 对照实验里"IC 加权组"
        # 跑的其实是先验权重，却被解读成"IC 加权无效"——**假结论比报错
        # 危险得多**。所以这里默认直接抛出；确实需要容错的场景
        # （如线上实盘不愿因研究产物缺失而中断）显式设
        # `ic_strict=False` 才会降级，且降级信息会写进 scorer 状态。
        # ---------------------------------------------------------------
        strict = getattr(m, "ic_strict", True)
        msg = (f"[打分] IC 权重加载失败（{type(exc).__name__}: {exc}）"
               f" <- {Path(p)}")
        if strict:
            raise RuntimeError(
                msg + "\n  已阻止静默降级：weight_source='ic' 时若静默退回先验权重，"
                      "回测/对照实验会得出错误结论。\n"
                      "  修复方式：检查 summary.csv 是否含 rank_icir 列"
                      "（可用 `python scripts/rebuild_summary.py` 从 ic_ts.parquet 重建）。\n"
                      "  如确需降级：设置 model.ic_strict=False。"
            ) from exc
        print(msg + "；ic_strict=False，降级为先验权重")
        scorer = FactorScorer(lib)
        scorer._ic_source = "prior(fallback)"
    return scorer


class FactorScorer:
    """把原始因子值转成 [0,1] 得分并加权合成。"""

    def __init__(
        self,
        library: FactorLibrary | None = None,
        weights: dict[str, float] | None = None,
        use_ic_weight: bool = False,
        ic_summary: "object | None" = None,
    ) -> None:
        self.library = library or FactorLibrary()
        self.weights: dict[str, float] = dict(weights or DEFAULT_WEIGHTS)
        self.use_ic_weight = use_ic_weight or ic_summary is not None
        self._ic_source: str = "prior"
        if ic_summary is not None:
            self.set_ic_weights(ic_summary)
        self._align_names()
        self._normalize_weights()

    # ------------------------------------------------------------ 权重配置
    def _align_names(self) -> None:
        """旧因子名 -> 新因子名；并丢弃库中不存在的因子。"""
        known = set(self.library.names)
        fixed: dict[str, float] = {}
        for k, v in self.weights.items():
            nk = normalize_name(k)
            if nk in known:
                fixed[nk] = fixed.get(nk, 0.0) + v
        # 库里有但权重表没有的因子，按组内均值补（避免新因子被静默忽略）
        for n in self.library.names:
            if n not in fixed:
                fixed[n] = 0.0
        self.weights = fixed

    def _normalize_weights(self) -> None:
        total = sum(abs(v) for v in self.weights.values())
        if total <= 0:
            # 全 0（新因子未定权）时退化为等权
            n = len(self.weights) or 1
            self.weights = {k: 1.0 / n for k in self.weights}
            total = 1.0
        self.weights = {k: v / total for k, v in self.weights.items()}

    def set_ic_weights(
        self,
        ic_summary,
        mode: str = "icir",
        min_abs_ic: float = 0.01,
        max_p: float = 0.10,
        cap: float = 0.25,
        shrink: float = 1.0,
        select: bool = True,
        corr: "object | None" = None,
        corr_threshold: float = 0.85,
        min_icir_ratio: float = 0.5,
    ) -> dict[str, float]:
        """用实测 IC 结果自动定权。

        参数
        ----
        ic_summary
            ``scripts/factor_research.py`` 产出的 summary（DataFrame 或 csv 路径），
            需含列 ``因子 / factor``、``rank_ic``、``rank_icir``、``rank_ic_p``。
            若带 ``icir_neu`` 列，会额外做"中性化抗性"检查（剔除风格依赖因子）。
        mode
            ``icir``（推荐，兼顾强度与稳定性）或 ``ic``（只看强度）。
        min_abs_ic / max_p
            同时满足 \\|RankIC\\| >= min_abs_ic 且 p <= max_p 的因子才纳入。
        cap
            单因子权重上限（绝对值），防止某一因子垄断组合。
        shrink
            幂次压缩：``w ∝ |v| ** shrink``。先用组内 min-max 归一化到 [0,1]
            再取幂，否则各因子的量纲（ICIR 约 0.2~0.6）会把幂次压缩变成
            几乎无差别的等权 —— **这正是 P2 首轮 A/B 对照失败的原因**。
        select / corr / corr_threshold
            是否做因子筛选（显著性 + 风格依赖 + 相关性去冗余）。
            强烈建议开启；关掉会让高相关因子重复计权。
        """
        import pandas as pd

        if isinstance(ic_summary, (str, Path)):
            df = pd.read_csv(ic_summary)
        else:
            df = ic_summary.copy()
        name_col = "因子" if "因子" in df.columns else ("factor" if "factor" in df.columns else None)
        if name_col is None:
            raise ValueError("ic_summary 缺少因子名列（'因子' 或 'factor'）")
        df[name_col] = df[name_col].astype(str).str.replace(r"_neu$", "", regex=True)
        df = df.set_index(name_col)

        val_col = "rank_icir" if mode == "icir" else "rank_ic"
        if val_col not in df.columns:
            # mode="icir" 却没有 rank_icir 列时**不能**悄悄退回 rank_ic：
            # 两者量纲差一个数量级（ICIR 约 0.2~0.6，RankIC 约 0.03~0.1），
            # 换了口径会让权重分布完全变样，而调用方毫不知情。
            # 这里显式报错，由 build_scorer 决定是抛出还是按 ic_strict 降级。
            if mode == "icir":
                raise KeyError(
                    f"summary 缺少 'rank_icir' 列（mode='icir' 必需）；"
                    f"实际列为 {list(df.columns)[:12]}..."
                    f" —— 可用 scripts/rebuild_summary.py 从 ic_ts.parquet 重建"
                )
            val_col = "rank_ic"
        ic_col = "rank_ic" if "rank_ic" in df.columns else "ic"
        p_col = "rank_ic_p" if "rank_ic_p" in df.columns else "ic_p"

        # ---- 筛选（显著性 + 中性化抗性 + 相关性去冗余）----
        selected: set[str] | None = None
        if select:
            from aq.factors.ic import select_factors

            corr_df = None
            if corr is not None:
                corr_df = pd.read_csv(corr, index_col=0) if isinstance(corr, (str, Path)) else corr
                corr_df.index = [str(i).replace("_neu", "") for i in corr_df.index]
                corr_df.columns = [str(c).replace("_neu", "") for c in corr_df.columns]
            if corr_df is None:
                # 未提供相关性矩阵时退化为"显著性 + 风格依赖"筛选
                keep_mask = (df[ic_col].abs() >= min_abs_ic) & (df[p_col] <= max_p)
                neu = df["icir_neu"] if "icir_neu" in df.columns else None
                if neu is not None:
                    ratio = neu.abs() / df[val_col].abs().replace(0, float("nan"))
                    keep_mask &= ratio >= min_icir_ratio
                selected = {str(x) for x in df.index[keep_mask]}
            else:
                tmp = df.reset_index().rename(columns={name_col: "factor"})
                keep, _ = select_factors(
                    tmp, corr_df,
                    min_abs_ic=min_abs_ic, max_p=max_p,
                    corr_threshold=corr_threshold, min_neg_icir_keep=min_icir_ratio,
                    name_col="factor", icir_col=val_col, ic_col=ic_col, p_col=p_col,
                )
                selected = set(keep)

        # ---- 强度 -> 权重 ----
        raw: dict[str, float] = {}
        for name, row in df.iterrows():
            nm = normalize_name(str(name))
            if nm not in self.weights:
                continue
            ic = _tofloat(row.get(ic_col))
            v = _tofloat(row.get(val_col))
            if selected is not None and nm not in selected:
                raw[nm] = 0.0
                continue
            if not (math.isfinite(ic) and math.isfinite(v)):
                raw[nm] = 0.0
                continue
            if abs(ic) < min_abs_ic or (math.isfinite(_tofloat(row.get(p_col))) and _tofloat(row.get(p_col)) > max_p):
                raw[nm] = 0.0
                continue
            raw[nm] = abs(v)

        for n in self.weights:
            raw.setdefault(n, 0.0)

        # ---- 组内 min-max 归一化 + 幂次压缩 ----
        vals = [v for v in raw.values() if v > 0]
        if vals:
            lo, hi = min(vals), max(vals)
            span = (hi - lo) or hi or 1.0
            scaled = {k: (((v - lo) / span) if v > 0 else 0.0) ** shrink for k, v in raw.items()}
        else:
            scaled = dict.fromkeys(raw, 0.0)

        # ---------------------------------------------------------------
        # **权重一律取正号**：因子的"好坏方向"由 `library.direction()` 在
        # `score_cross_section` 里统一施加（z-score 时乘 flip），
        # 这里如果再按实测 IC 的符号翻一次，方向就被判定了两遍。
        #
        # 这正是 P2 第二轮 A/B 对照失败的根因：A 股是反转市，实测 IC
        # 大多为负，于是 20 个因子里 17 个被赋成负权重；而它们中多数
        # 的 `direction` 本就是 +1（库先验认为"越高越好"）。
        # 两次翻转叠加后，组合实际是在**反向打分**——夏普和胜率略有
        # 改善（因为 A 股确实反转），但收益与回撤明显恶化，整体不如
        # 符号干净的先验权重。
        #
        # 若要"用实测 IC 纠正先验方向"，正确做法是改 `FactorSpec.direction`
        # （或在校准阶段写回），让方向**只有一个事实来源**，而不是在
        # 权重上叠加第二层符号。
        # ---------------------------------------------------------------
        signed = {k: abs(scaled[k]) for k in raw}

        # 记录实测 IC 与库先验方向是否冲突（供审计，不影响权重）
        self._direction_conflicts = {}
        for k in raw:
            if raw[k] <= 0:
                continue
            icv = _tofloat(df.loc[k, ic_col]) if k in df.index else float("nan")
            if not math.isfinite(icv):
                continue
            lib_dir = self.library.direction(k)
            if (icv > 0) != (lib_dir > 0):
                self._direction_conflicts[k] = {
                    "ic_sign": 1 if icv > 0 else -1,
                    "lib_direction": lib_dir,
                }
        if self._direction_conflicts:
            names = ", ".join(sorted(self._direction_conflicts))
            print(f"[打分][注意] {len(self._direction_conflicts)} 个因子的实测 IC 与库先验方向冲突："
                  f"{names}")
            print("[打分][注意] 已被忽略（方向由 library.direction 唯一决定）；"
                  "如需以实测为准，请改 FactorSpec.direction")

        # ---- 单因子上限（迭代截断 + 按比例重分配）----
        new_w = signed
        total_abs = sum(abs(v) for v in new_w.values())
        if total_abs > 0:
            new_w = {k: v / total_abs for k, v in new_w.items()}
            for _ in range(10):
                excess = 0.0
                for k in new_w:
                    if abs(new_w[k]) > cap:
                        excess += abs(new_w[k]) - cap
                        new_w[k] = math.copysign(cap, new_w[k])
                if excess <= 1e-9:
                    break
                rest = [k for k in new_w if abs(new_w[k]) < cap - 1e-12]
                if not rest:
                    break
                tot_rest = sum(abs(new_w[k]) for k in rest) or 1.0
                for k in rest:
                    new_w[k] += math.copysign(excess * abs(new_w[k]) / tot_rest, new_w[k])

        self.weights = new_w
        self.use_ic_weight = True
        self._ic_source = f"ic:{mode}" + ("+select" if selected is not None else "")
        self._normalize_weights()
        return dict(self.weights)

    @property
    def direction_conflicts(self) -> dict:
        """实测 IC 与库先验方向冲突的因子（审计用，不影响权重）。"""
        return dict(getattr(self, "_direction_conflicts", {}))

    @property
    def ic_source(self) -> str:
        """当前权重来源：``prior`` 或 ``ic:icir``。"""
        return self._ic_source

    def active_factors(self) -> list[str]:
        """实际参与打分的因子（权重非零）。"""
        return [k for k, v in self.weights.items() if abs(v) > 1e-9]

    # ------------------------------------------------------------- 横截面
    def score_cross_section(
        self,
        factor_map: dict[str, dict[str, float | None]],
        model_version: str = "factor-v1",
    ) -> list[Prediction]:
        """输入 {symbol: {factor: value}}，输出按评分降序的 Prediction 列表。"""
        symbols = list(factor_map)
        if not symbols:
            return []

        names = [n for n in self.library.names if n in self.weights]

        # 1) 每个因子在横截面上做 z-score
        z_map: dict[str, dict[str, float]] = {}
        for fname in names:
            vals = [factor_map[s].get(fname) for s in symbols]
            valid = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
            if len(valid) < 2:
                z_map[fname] = {s: 0.0 for s in symbols}
                continue
            mu, sd = mean(valid), pstdev(valid)
            sd = sd or 1.0
            flip = self.library.direction(fname)
            z_map[fname] = {
                s: (flip * ((factor_map[s].get(fname) or mu) - mu) / sd)
                for s in symbols
            }

        # 2) z -> 定向得分 s ∈ [0,1]
        score_map: dict[str, dict[str, float]] = {}
        for fname in names:
            w = self.weights.get(fname, 0.0)
            sgn = 1.0 if w >= 0 else -1.0
            score_map[fname] = {
                s: 0.5 * (1.0 + sgn * math.tanh(z_map[fname][s] / 2.0))
                for s in symbols
            }

        # 3) 加权合成（Σ|w| = 1，故 score ∈ [0,1]）
        out: list[Prediction] = []
        for s in symbols:
            contribs: list[FactorContrib] = []
            total = 0.0
            for fname in names:
                w = self.weights.get(fname, 0.0)
                if abs(w) < 1e-12:
                    continue
                val = score_map[fname][s]
                contrib = abs(w) * val
                total += contrib
                contribs.append(
                    FactorContrib(name=fname, weight=w, value=round(val, 4), contrib=contrib)
                )

            conf = self._confidence(contribs, total)
            direction = "BUY" if total >= 0.55 else ("SELL" if total <= 0.45 else "HOLD")

            top = sorted(contribs, key=lambda c: c.contrib, reverse=True)[:3]
            explain = "主要驱动：" + "、".join(f"{c.name}({c.value:.2f})" for c in top)

            out.append(
                Prediction(
                    symbol=s,
                    score=round(total, 6),
                    confidence=round(conf, 4),
                    direction=direction,  # type: ignore[arg-type]
                    factor_contrib=contribs,
                    model_version=model_version,
                    explain=explain,
                )
            )

        out.sort(key=lambda p: p.score, reverse=True)
        return out

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _confidence(contribs: list[FactorContrib], total: float) -> float:
        """置信度：得分偏离中性的程度 + 因子方向一致性。"""
        if not contribs:
            return 0.0
        vals = [c.value for c in contribs]
        avg = mean(vals) if vals else 0.0
        agree = 1.0 - min(1.0, pstdev(vals) / 0.5) if len(vals) > 1 else 0.5
        dev = min(1.0, abs(avg - 0.5) * 2.0)
        return 0.5 * agree + 0.5 * dev
