"""因子库 —— 标量封装层（实时 / API 用）。

**P2 重构说明**：因子算法本身已全部下沉到 :mod:`aq.factors.vlib`（向量化内核），
本模块只负责把 ``list[Bar]`` 转成 DataFrame、调用内核、取最后一行。

这样做的原因：研究和实盘必须共用同一套算法，否则"回测里有效的因子，上线后算出来
是另一个值"——这是量化系统最隐蔽也最致命的一类 bug。

对外 API 与 P0/P1 保持一致：``FactorLibrary.compute(symbol, bars) -> {name: value}``。
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np
import pandas as pd

from aq.core.models import Bar
from aq.factors.vlib import FACTOR_SPECS, REQUIRES_SHARES, SPEC_BY_NAME, spec_names

FactorFn = Callable[[list[Bar]], float | None]

# 旧因子名 -> 新因子名（配置文件迁移用；旧配置不会因此报错）
LEGACY_ALIAS: dict[str, str] = {
    "momentum_20": "mom_20",
    "momentum_60": "mom_60",
    "reversal_5": "rev_5",
    "reversal_20": "rev_20",
    "volatility_20": "vol_20",
    "volatility_60": "vol_60",
    "ma_bias_20": "ma_bias_20",
    "ma_cross_5_20": "ma_cross_5_20",
    "volume_ratio": "vol_ratio",
    "turnover_change": "turnover_chg",
    "days_since_high": "days_since_high_60",
    "limit_up_count": "limit_up_cnt_20",
    "gap": "gap",
}


def normalize_name(name: str) -> str:
    """把旧因子名映射成当前名字（已是新名字则原样返回）。"""
    return LEGACY_ALIAS.get(name, name)


# ---------------------------------------------------------------------------
# Bar <-> DataFrame
# ---------------------------------------------------------------------------


def bars_to_frame(bars: list[Bar], float_share: float | None = None) -> pd.DataFrame:
    """把 Bar 列表转成内核需要的 DataFrame。"""
    if not bars:
        return pd.DataFrame()
    recs = []
    for b in bars:
        recs.append(
            {
                "time": b.time,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "amount": b.amount,
                "pre_close": b.pre_close if b.pre_close is not None else np.nan,
                "limit_up": b.limit_up if b.limit_up is not None else np.nan,
                "limit_down": b.limit_down if b.limit_down is not None else np.nan,
            }
        )
    df = pd.DataFrame(recs)
    if float_share:
        df["float_share"] = float(float_share)
    return df


def _last(v) -> float | None:
    """取最后一个有效值；NaN / inf / 全空 -> None。"""
    try:
        if v is None:
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class FactorLibrary:
    """因子注册表：名称 -> 计算函数（标量版，吃 ``list[Bar]``）。

    因子定义来自 :data:`aq.factors.vlib.FACTOR_SPECS`，本类只做适配。
    """

    def __init__(self, names: list[str] | None = None) -> None:
        self._specs = [s for s in FACTOR_SPECS if names is None or s.name in set(names)]
        self._factors: dict[str, FactorFn] = {}
        self._directions: dict[str, int] = {}
        self._groups: dict[str, str] = {}
        self._descs: dict[str, str] = {}
        self.float_shares: dict[str, float] = {}  # symbol -> 流通股本（换手率因子用）
        self._register()

    def _register(self) -> None:
        for spec in self._specs:
            self._factors[spec.name] = self._make_fn(spec.name)
            self._directions[spec.name] = spec.direction
            self._groups[spec.name] = spec.group
            self._descs[spec.name] = spec.desc

    def _make_fn(self, name: str) -> FactorFn:
        spec = SPEC_BY_NAME[name]

        def fn(bars: list[Bar]) -> float | None:
            if not bars:
                return None
            df = bars_to_frame(bars)
            if df.empty:
                return None
            try:
                series = spec.fn(df)
            except Exception:  # noqa: BLE001
                return None
            if series is None or len(series) == 0:
                return None
            return _last(np.asarray(series, dtype="float64")[-1])

        fn.__name__ = f"factor_{name}"
        return fn

    # ------------------------------------------------------------------ API
    def register(self, name: str, fn: FactorFn, direction: int = 1, group: str = "自定义") -> None:
        """注册自定义因子（会覆盖同名内置因子）。"""
        self._factors[name] = fn
        self._directions[name] = direction
        self._groups[name] = group
        self._descs[name] = group

    @property
    def names(self) -> list[str]:
        return [s.name for s in self._specs]

    @property
    def specs(self):
        return list(self._specs)

    def direction(self, name: str) -> int:
        return self._directions.get(normalize_name(name), 1)

    def group(self, name: str) -> str:
        return self._groups.get(normalize_name(name), "")

    def desc(self, name: str) -> str:
        return self._descs.get(normalize_name(name), "")

    def compute(self, symbol: str, bars: list[Bar], float_share: float | None = None) -> dict[str, float | None]:
        """计算单只股票的全部因子值（取最后一根 bar）。

        需要股本的因子（换手率）在股本缺失时返回 None，不会污染其他因子。
        """
        out: dict[str, float | None] = {}
        if not bars:
            return {n: None for n in self._factors}

        fs = float_share if float_share is not None else self.float_shares.get(symbol)
        df = bars_to_frame(bars, float_share=fs)

        for name in self._factors:
            if name in REQUIRES_SHARES and (not fs or "float_share" not in df):
                out[name] = None
                continue
            try:
                series = SPEC_BY_NAME[name].fn(df)
                out[name] = _last(np.asarray(series, dtype="float64")[-1])
            except Exception:  # noqa: BLE001
                out[name] = None
        return out

    def compute_frame(self, df: pd.DataFrame, names: list[str] | None = None) -> pd.DataFrame:
        """向量化入口：整段 DataFrame -> 因子矩阵（P2 面板构建用）。"""
        from aq.factors.vlib import compute_all

        return compute_all(df, names)


def compute_factors(
    symbol: str,
    bars: list[Bar],
    library: FactorLibrary | None = None,
    float_share: float | None = None,
) -> dict[str, float | None]:
    lib = library or FactorLibrary()
    return lib.compute(symbol, bars, float_share=float_share)


__all__ = [
    "FactorLibrary",
    "compute_factors",
    "bars_to_frame",
    "spec_names",
    "LEGACY_ALIAS",
    "normalize_name",
]
