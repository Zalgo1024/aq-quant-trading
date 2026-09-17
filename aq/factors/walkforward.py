# -*- coding: utf-8 -*-
"""Walk-forward 定权：把 IC 权重也变成样本外变量。

为什么必须做
-----------
7.13 的 CSCV 给出了"没有可证实优势"的结论，但它有一条明确的局限：
喂进去的日收益**并不是真正样本外的**。因为回测用的 IC 权重
（``full_neu_sh``）是在**整段 2019~2026 历史**上拟合出来的，而回测只跑
2021~2026 —— 权重见过未来。所以 7.13 的 PBO 只度量了"在 6 个结构参数里
挑选"带来的过拟合，**不含因子筛选与定权本身的过拟合**，是总过拟合的下界。

这个模块把定权也搬到滚动窗口外：

    t 时刻的权重 = 只用 t 之前 W 个交易日的信息重新筛一遍、定一遍

这样产生的日收益序列才是真正样本外的，可以诚实地喂给 CSCV / DSR。

三个"不这么写就会悄悄前视"的地方
--------------------------------
1. **IC 是逐日横截面统计量，不依赖估计窗口。** 所以整段历史的日 IC 时序
   可以一次性算好再切片 —— 任意回看窗口的 RankIC / ICIR / NW-t / p
   都只是它的子集统计量。等价于"每个 refit 点重算一遍"，但快几个数量级。
2. **IC 自带前瞻。** 面板里 ``fwd_ret_1 = close[t+1]/close[t] - 1``，
   所以 IC[t] 要等 t+1 收盘才可知。refit 发生在第 d 日时只能用
   ``ic_ts.index <= d - (horizon + 1)``。这里刻意多留一天缓冲
   （``LAG_EXTRA = 1``）—— 宁可保守，也不能让"样本外"打折扣，
   否则整个检验就白做了。
3. **相关性去冗余矩阵也必须滚动。** 直接复用全样本 ``corr.csv``
   等于把未来又搬了回来。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from aq.factors.ic import newey_west_t

# 前瞻缓冲：horizon 之外再多留一天。
# horizon=1 时，第 d 日 refit 最多用到 IC[d-3]，即三个交易日前的信息。
LAG_EXTRA = 1


# --------------------------------------------------------------------------
# 滚动 IC 汇总
# --------------------------------------------------------------------------


def _strip_neu(col: str) -> str:
    """归一因子名：``mom_20_neu__rankic`` / ``mom_20_neu`` → ``mom_20``。

    两种输入都要能吃下 —— 调用方有时传完整列名，有时传已经去掉
    ``__rankic`` 的因子列名。第一版只处理了前者，于是后者会原样返回
    ``mom_20_neu``，去 raw 变体里找 ``mom_20_neu__rankic`` 永远找不到，
    **icir_raw 静默为空**（中性化抗性门控又退化成恒真）。
    """
    s = col
    for suf in ("__rankic", "__ic", "__n"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s[:-4] if s.endswith("_neu") else s


def _as_dt_index(df: pd.DataFrame) -> pd.DataFrame:
    """把索引归一成 DatetimeIndex 并排序。

    ``ic_ts.parquet`` 存下来再读回时索引是字符串，而 ``searchsorted`` 拿
    ``pd.Timestamp`` 去查字符串索引不会按时间比较 —— 那会让"可用窗口"
    错位，也就是悄悄引入前视。所以这里统一归一。
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index)
    return df.sort_index()


def rolling_ic_summary(
    ic_sub: pd.DataFrame,
    factor_cols: list[str],
    horizon: int = 1,
    nw_lags: int | None = None,
    ic_sub_raw: pd.DataFrame | None = None,
    min_days: int = 120,
) -> pd.DataFrame:
    """把某个回看窗口内的日 IC 汇总成 ``summary``（列契约同 factor_research）。

    ``ic_sub_raw`` 传入**未中性化**变体的同一窗口 IC 时，会额外算出
    ``icir_raw`` 列 —— 这是"中性化抗性门控"唯一正确的分母，缺了它那道门
    在 full_neu 变体下比值恒为 1，形同虚设（见 aq/factors/ic.py 的注释）。
    """
    rows: list[dict[str, Any]] = []
    lags = nw_lags if nw_lags is not None else max(horizon, 4)

    for c in factor_cols:
        ric = ic_sub.get(f"{c}__rankic")
        ic = ic_sub.get(f"{c}__ic")
        if ric is None or ic is None:
            continue
        ric = ric.dropna()
        ic = ic.dropna()
        if len(ric) < min_days:
            # 样本太短就不给这个因子投票（权重 0），而不是用一个噪声很大的估计
            continue

        t, p = newey_west_t(ric, lags=lags)
        it, ip = newey_west_t(ic, lags=lags)
        rm, rs = float(ric.mean()), float(ric.std(ddof=1))
        im, isd = float(ic.mean()), float(ic.std(ddof=1))

        base = _strip_neu(c)
        row: dict[str, Any] = {
            "因子": base,
            "n_days": int(len(ric)),
            "rank_ic": rm,
            "rank_ic_std": rs,
            "rank_icir": rm / rs if rs else float("nan"),
            "rank_ic_t": t,
            "rank_ic_p": p,
            "ic": im,
            "ic_std": isd,
            "icir": im / isd if isd else float("nan"),
            "ic_t": it,
            "ic_p": ip,
            "pos_ratio": float((ric > 0).mean()),
            "direction": 1 if rm >= 0 else -1,
            "icir_neu": rm / rs if rs else float("nan"),
        }
        if ic_sub_raw is not None:
            rx = ic_sub_raw.get(f"{base}__rankic")
            if rx is not None:
                rx = rx.dropna()
                if len(rx) >= min_days:
                    xm, xs = float(rx.mean()), float(rx.std(ddof=1))
                    row["icir_raw"] = xm / xs if xs else float("nan")
        rows.append(row)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def rolling_ic_corr(
    ic_sub: pd.DataFrame,
    factor_cols: list[str],
    min_overlap: int = 120,
) -> pd.DataFrame:
    """用**日 IC 时序**的相关性做去冗余矩阵。

    为什么不是因子值的相关性
    ------------------------
    定权要处理的是"两个因子是否在传递同一条信息"，而这正是 IC 时序
    相关性直接度量的东西；因子值相关性只是它的一个代理。
    更实际的理由：IC 时序本身就够小、够干净、且天然是滚动的 ——
    不必为了算一次相关性去加载几百 MB 的面板，也就不会因为怕慢而
    退回用全样本 ``corr.csv``（那会把未来带回来）。

    与全样本因子值相关性的差异会体现在"剔除哪 4 个冗余因子"上，
    所以 walk-forward 的结果与 7.13 的静态基线**在两个维度上同时不同**：
    权重滚动 + 去冗余口径。要分离这两个效应，见 run 脚本的 ``--corr static``。
    """
    cols = [c for c in factor_cols if f"{c}__rankic" in ic_sub.columns]
    if len(cols) < 2:
        return pd.DataFrame()
    d = ic_sub[[f"{c}__rankic" for c in cols]]
    cnt = d.notna().sum()
    keep = [c for c in cols if cnt[f"{c}__rankic"] >= min_overlap]
    if len(keep) < 2:
        return pd.DataFrame()
    m = ic_sub[[f"{c}__rankic" for c in keep]]
    corr = m.corr(method="spearman", min_periods=min_overlap)
    names = [_strip_neu(c) for c in keep]
    corr.index = names
    corr.columns = names
    return corr


# --------------------------------------------------------------------------
# 滚动定权器
# --------------------------------------------------------------------------


class WalkForwardWeighter:
    """按交易日步进地在回看窗口内重新筛因子、定权重。

    用法（引擎侧）：每个交易日打分前调一次 :meth:`maybe_refit`，
    到点就地把新权重推给 ``FactorScorer``。
    """

    def __init__(
        self,
        cfg: Any,
        ic_ts: pd.DataFrame,
        ic_ts_raw: pd.DataFrame | None = None,
        corr_static: pd.DataFrame | None = None,
        corr_mode: str = "ic",
        horizon: int = 1,
        nw_lags: int | None = None,
        min_days: int = 120,
        verbose: bool = True,
    ) -> None:
        m = cfg.model
        self.window = int(getattr(m, "wf_window", 504))          # 回看窗口（交易日）
        self.refit_every = int(getattr(m, "wf_refit_every", 60))  # 重新定权间隔
        self.corr_mode = corr_mode
        self.horizon = int(horizon)
        self.lag = self.horizon + LAG_EXTRA
        self.nw_lags = nw_lags
        self.min_days = int(min_days)
        self.verbose = verbose

        # 定权超参（从 cfg 读，保证与静态基线用的是同一套门限）
        self.ic_kwargs: dict[str, Any] = {
            "mode": getattr(m, "ic_weight_mode", "icir"),
            "min_abs_ic": getattr(m, "ic_min_abs", 0.01),
            "max_p": getattr(m, "ic_max_p", 0.10),
            "cap": getattr(m, "ic_cap", 0.25),
            "shrink": getattr(m, "ic_shrink", 1.0),
            "select": getattr(m, "ic_select", True),
            "corr_threshold": getattr(m, "ic_corr_threshold", 0.85),
            "min_icir_ratio": getattr(m, "ic_min_icir_ratio", 0.5),
        }

        # ---- 成本感知定权（B1）在 walk-forward 下**主动拒绝** ----
        # 原因：这里的 summary 由 `rolling_ic_summary` 从 ic_ts.parquet 的
        # **日 IC 序列**汇总而来，**不含 turnover 列**。而 turnover 必须与 IC
        # 用同一个回看窗口估计；若拿 summary.csv 的全样本换手去定窗口内权重，
        # 就是前视 —— 恰恰是 walk-forward 存在的意义所在。
        #
        # 宁可不支持，也不能悄悄用静态换手冒充"滚动验收"（本项目已经吃过
        # 两次"配置看起来能调、其实调不动 / 静默降级"的亏）。
        # 补法：给 factor_research 增加逐日换手序列并写进 ic_ts.parquet。
        _cp = float(getattr(m, "ic_cost_penalty", 0.0) or 0.0)
        if _cp:
            raise NotImplementedError(
                "ic_cost_penalty != 0 暂不支持 weight_source='ic_wf'：\n"
                "  滚动窗口的 summary 没有 turnover 列，无法与 IC 保持同一窗口口径；\n"
                "  用全样本换手会引入前视，等于把 walk-forward 的意义抹掉。\n"
                "  请先用 weight_source='ic'（静态定权）做第一轮筛查，\n"
                "  或先给 ic_ts.parquet 补一列逐日换手序列。")

        # 只保留中性化变体里存在的因子列（其余是别的变体带来的）
        self.factor_cols = sorted({c[: -len("__rankic")]
                                   for c in ic_ts.columns if c.endswith("__rankic")})
        # ⚠️ ic_ts.parquet 落盘后索引是**字符串**而不是 DatetimeIndex。
        # 不转的话 searchsorted(pd.Timestamp) 会静默给出错误位置（或直接抛错），
        # 而"切片位置错了"正是会悄悄造成前视的那类问题，必须显式归一。
        self.ic_ts = _as_dt_index(ic_ts)
        self.ic_ts_raw = _as_dt_index(ic_ts_raw) if ic_ts_raw is not None else None
        self.corr_static = corr_static

        self._last_hi: int | None = None
        self._first = True
        self.history: list[dict[str, Any]] = []
        if verbose:
            print(f"[WF] 滚动定权：窗口 {self.window} 日 / 每 {self.refit_every} 日重定一次"
                  f" / 前瞻缓冲 {self.lag} 日 / 相关性口径 {self.corr_mode}")
            print(f"[WF] 可用 IC 时序 {len(self.ic_ts)} 日"
                  f"（{self.ic_ts.index[0].date()} ~ {self.ic_ts.index[-1].date()}），"
                  f"{len(self.factor_cols)} 个因子")

    # ---------------------------------------------------------------- 内部
    def _slice(self, ds: str) -> tuple[int, int] | None:
        """算出当前时点可用的 IC 窗口 ``[lo, hi)``；不足则返回 None。"""
        d = pd.Timestamp(ds)
        # searchsorted(left)：第一个 >= d 的位置。idx[:p] 严格早于 d。
        p = int(self.ic_ts.index.searchsorted(d, side="left"))
        hi = p - self.lag          # 扣掉前瞻缓冲
        if hi < self.min_days:
            return None
        lo = max(0, hi - self.window)
        return lo, hi

    def _due(self, hi: int) -> bool:
        """是否到了重定权时点。"""
        if self._first:
            return True
        return self._last_hi is not None and (hi - self._last_hi) >= self.refit_every

    # ---------------------------------------------------------------- 外部
    def maybe_refit(self, ds: str, scorer) -> bool:  # type: ignore[no-untyped-def]
        """到了时点就重定权，返回是否真的重定了。"""
        sl = self._slice(ds)
        if sl is None:
            return False
        lo, hi = sl
        if not self._due(hi):
            return False

        ic_sub = self.ic_ts.iloc[lo:hi]
        ic_raw_sub = (self.ic_ts_raw.iloc[
            max(0, lo):hi] if self.ic_ts_raw is not None else None)
        summary = rolling_ic_summary(
            ic_sub, self.factor_cols, horizon=self.horizon,
            nw_lags=self.nw_lags, ic_sub_raw=ic_raw_sub, min_days=self.min_days,
        )
        if summary.empty:
            return False

        if self.corr_mode == "static":
            corr = self.corr_static
        else:
            corr = rolling_ic_corr(ic_sub, self.factor_cols, min_overlap=self.min_days)

        scorer.set_ic_weights(summary, corr=corr, **self.ic_kwargs)

        self._first = False
        self._last_hi = hi
        self.history.append({
            "asof": str(ds),
            "ic_from": str(ic_sub.index[0].date()),
            "ic_to": str(ic_sub.index[-1].date()),
            "n_ic_days": int(len(ic_sub)),
            "n_factors_weighted": len(scorer.active_factors()),
            "weights": {k: round(float(v), 6)
                        for k, v in scorer.weights.items() if abs(float(v)) > 1e-9},
        })
        if self.verbose:
            print(f"    [WF] {ds} 重定权 ← IC {ic_sub.index[0].date()}"
                  f"~{ic_sub.index[-1].date()}（{len(ic_sub)} 日）"
                  f" → {len(scorer.active_factors())} 个因子")
        return True

    def summary_frame(self) -> pd.DataFrame:
        """每次重定权的记录（含权重明细），用于落盘与事后审计。"""
        if not self.history:
            return pd.DataFrame()
        rows = []
        for h in self.history:
            # 浅拷贝再取 weights：直接 pop 会把 history 里的记录改坏，
            # 后续再想看原始权重就没了
            d = dict(h)
            w = d.pop("weights", {})
            rows.append({**d, "weights": json.dumps(w, ensure_ascii=False)})
        return pd.DataFrame(rows)
