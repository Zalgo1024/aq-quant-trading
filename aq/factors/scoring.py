"""因子打分器：横截面标准化 + 加权复合评分。

**直接复用彩票项目的思路**：
    综合评分 = Σ (因子归一化得分 × 权重)
并输出每个因子的贡献明细，供前端做"因子归因/瀑布图"。

差异点（比彩票版更严谨）：
- 彩票用固定权重（0.4/0.35/0.25），这里支持：
  1) 等权；2) 指定权重；3) IC 加权（接入验证结果后自动计算）。
- 支持因子方向（越小越好时自动取反）。
"""

from __future__ import annotations

import math
from statistics import mean, pstdev

from aq.core.models import FactorContrib, Prediction
from aq.factors.library import FactorLibrary

DEFAULT_WEIGHTS: dict[str, float] = {
    "momentum_20": 0.12,
    "reversal_5": 0.15,
    "volatility_20": 0.08,
    "ma_bias_20": 0.10,
    "ma_cross_5_20": 0.10,
    "volume_ratio": 0.10,
    "turnover_change": 0.10,
    "days_since_high": 0.05,
    "limit_up_count": 0.10,
    "gap": 0.10,
}


class FactorScorer:
    """把原始因子值转成 [0,1] 得分并加权合成。

    横截面标准化：对每个因子在所有股票上做 z-score，再映射到 [0,1]。
    """

    def __init__(
        self,
        library: FactorLibrary | None = None,
        weights: dict[str, float] | None = None,
        use_ic_weight: bool = False,
    ) -> None:
        self.library = library or FactorLibrary()
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        self.use_ic_weight = use_ic_weight
        self._normalize_weights()

    def _normalize_weights(self) -> None:
        total = sum(abs(v) for v in self.weights.values()) or 1.0
        self.weights = {k: v / total for k, v in self.weights.items()}

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

        # 1) 每个因子在横截面上做 z-score
        z_map: dict[str, dict[str, float]] = {}
        for fname in self.library.names:
            vals = [factor_map[s].get(fname) for s in symbols]
            valid = [v for v in vals if v is not None and not math.isnan(v)]
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

        # 2) 每个因子的 z -> [0,1]（用 tanh 压缩，避免极值主导）
        score_map: dict[str, dict[str, float]] = {}
        for fname in self.library.names:
            score_map[fname] = {
                s: 0.5 * (1.0 + math.tanh(z_map[fname][s] / 2.0)) for s in symbols
            }

        # 3) 加权合成
        # 注意：先用全精度累加得到 total，最后再统一 round，
        # 否则 contrib（已 round）之和与 total 会有舍入差。
        out: list[Prediction] = []
        for s in symbols:
            contribs: list[FactorContrib] = []
            total = 0.0
            for fname, w in self.weights.items():
                if fname not in score_map:
                    continue
                val = score_map[fname][s]
                contrib = w * val
                total += contrib
                contribs.append(
                    FactorContrib(name=fname, weight=w, value=round(val, 4), contrib=contrib)
                )

            # 置信度：因子一致度（贡献越集中/方向越一致，置信度越高）
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
