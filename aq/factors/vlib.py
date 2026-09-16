"""向量化因子内核（P2）。

设计要点
--------
**这是因子计算的唯一权威实现。** 两个使用方共用它：

* ``aq.factors.panel``   —— 研究/回测：直接吃整段 DataFrame，向量化算全历史；
* ``aq.factors.library`` —— 实时/API：吃 ``list[Bar]``，转成 DataFrame 后取最后一行。

这样「研究算出来的因子」和「实盘打分的因子」不可能对不上（P0 时代最容易踩的坑）。

每个内核签名统一::

    def kernel(d: pd.DataFrame) -> pd.Series

``d`` 至少含列：open/high/low/close/volume/amount/pre_close/limit_up/limit_down。
返回值与 ``d`` 等长，数据不足处为 NaN。

**关于方向（direction）**：这是先验，不是结论。``+1`` 表示"因子值越大越看好"，
``-1`` 相反。真实方向由 ``aq.factors.ic`` 实测后自动校正（见 scoring 的
``auto_direction``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

TRADING_DAYS = 242  # A 股年化交易日


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _s(x) -> pd.Series:
    return pd.Series(np.asarray(x, dtype="float64"))


def _ret(close: pd.Series) -> pd.Series:
    """日收益率。用**后复权价**计算 —— 除权日自动连续。"""
    return close / close.shift(1) - 1.0


def _safe(a: pd.Series, b: pd.Series) -> pd.Series:
    """a / b，b 为 0 或 NaN 时返回 NaN。"""
    b2 = b.replace(0.0, np.nan)
    return a / b2


def _roll_max(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).max()


def _roll_min(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).min()


# ---------------------------------------------------------------------------
# 动量 / 反转
# ---------------------------------------------------------------------------


def k_mom(d: pd.DataFrame, n: int) -> pd.Series:
    """N 日动量：close_t / close_{t-n} - 1。"""
    c = _s(d["close"])
    return c / c.shift(n) - 1.0


def k_rev(d: pd.DataFrame, n: int) -> pd.Series:
    """N 日反转：-1 × N 日动量。A 股短期反转效应显著。"""
    return -k_mom(d, n)


def k_mom_risk_adj(d: pd.DataFrame, n: int = 60) -> pd.Series:
    """风险调整动量：N 日收益 / N 日波动（夏普式动量，比裸动量稳）。"""
    c = _s(d["close"])
    r = _ret(c)
    mom = c / c.shift(n) - 1.0
    vol = r.rolling(n, min_periods=max(5, n // 2)).std(ddof=0) * np.sqrt(TRADING_DAYS)
    return _safe(mom, vol)


# ---------------------------------------------------------------------------
# 波动 / 风险
# ---------------------------------------------------------------------------


def k_vol(d: pd.DataFrame, n: int) -> pd.Series:
    """N 日年化波动率。"""
    r = _ret(_s(d["close"]))
    return r.rolling(n, min_periods=max(5, n // 2)).std(ddof=0) * np.sqrt(TRADING_DAYS)


def k_downside_vol(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """下行波动率：只统计负收益的波动（比总波动更贴合"亏钱风险"）。"""
    r = _ret(_s(d["close"]))
    neg = r.clip(upper=0.0)
    return neg.rolling(n, min_periods=max(5, n // 2)).std(ddof=0) * np.sqrt(TRADING_DAYS)


def k_max_drawdown(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """近 N 日最大回撤（取正数：越大表示回撤越深）。"""
    c = _s(d["close"])
    peak = _roll_max(c, n)
    dd = c / peak - 1.0
    mdd = dd.rolling(n, min_periods=max(5, n // 2)).min()
    return -mdd


def k_skew(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """近 N 日收益偏度。"""
    r = _ret(_s(d["close"]))
    return r.rolling(n, min_periods=n).skew()


def k_hl_range(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """平均振幅 (high-low)/close，波动的极差口径。"""
    rng = (_s(d["high"]) - _s(d["low"])) / _s(d["close"])
    return rng.rolling(n, min_periods=max(5, n // 2)).mean()


# ---------------------------------------------------------------------------
# 趋势 / 均线
# ---------------------------------------------------------------------------


def _ma(c: pd.Series, n: int) -> pd.Series:
    return c.rolling(n, min_periods=max(3, n // 2)).mean()


def k_ma_bias(d: pd.DataFrame, n: int) -> pd.Series:
    """均线乖离率：close / MA_N - 1。"""
    c = _s(d["close"])
    return c / _ma(c, n) - 1.0


def k_ma_cross(d: pd.DataFrame, short: int = 5, long: int = 20) -> pd.Series:
    """均线交叉：MA_short / MA_long - 1。"""
    c = _s(d["close"])
    return _ma(c, short) / _ma(c, long) - 1.0


def k_rsi(d: pd.DataFrame, n: int = 14) -> pd.Series:
    """RSI（Wilder 平滑）。"""
    c = _s(d["close"])
    delta = c.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / n, adjust=False).mean()
    rs = _safe(gain, loss)
    return 100.0 - 100.0 / (1.0 + rs)


def k_stoch_pos(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """随机指标位置：(close - min low) / (max high - min low)，0~1。"""
    c, hi, lo = _s(d["close"]), _s(d["high"]), _s(d["low"])
    lmin, hmax = _roll_min(lo, n), _roll_max(hi, n)
    return _safe(c - lmin, hmax - lmin)


# ---------------------------------------------------------------------------
# 量能
# ---------------------------------------------------------------------------


def k_volume_ratio(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """量比：当日量 / 前 N 日均量（不含当日，避免自相关）。"""
    v = _s(d["volume"])
    base = v.shift(1).rolling(n, min_periods=max(5, n // 2)).mean()
    return _safe(v, base)


def k_turnover_change(d: pd.DataFrame, short: int = 5, long: int = 20) -> pd.Series:
    """换手突变：近 short 日均量 / 近 long 日均量。"""
    v = _s(d["volume"])
    return _safe(v.rolling(short, min_periods=2).mean(), v.rolling(long, min_periods=max(5, long // 2)).mean())


def k_amihud(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """Amihud 非流动性：mean(|ret| / amount) × 1e8。

    值越大 = 单位成交额引起的价格冲击越大 = 越缺乏流动性。
    A 股实证：非流动性与未来收益正相关（流动性溢价），故先验方向 +1。
    """
    r = _ret(_s(d["close"])).abs()
    amt = _s(d["amount"]).replace(0.0, np.nan)
    illiq = (r / amt) * 1e8
    return illiq.rolling(n, min_periods=max(5, n // 2)).mean()


def k_amount_log(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """近 N 日日均成交额取对数 —— 规模/关注度的代理（小成交额有溢价）。"""
    amt = _s(d["amount"]).replace(0.0, np.nan)
    return np.log(amt.rolling(n, min_periods=max(5, n // 2)).mean())


def k_up_down_vol(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """量能方向：上涨日均量 / 下跌日均量（>1 表示放量上涨、缩量下跌）。

    注意：up/dn 是**二值掩码**后的序列，窗口内有效样本只有约一半，
    min_periods 必须按 n/4 而不是 n/2 设，否则近期值会整片变成 NaN。
    """
    r = _ret(_s(d["close"]))
    v = _s(d["volume"])
    up = v.where(r > 0)
    dn = v.where(r < 0)
    mp = max(3, n // 4)
    return _safe(
        up.rolling(n, min_periods=mp).mean(),
        dn.rolling(n, min_periods=mp).mean(),
    )


def k_vol_price_corr(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """量价相关：近 N 日 corrcoef(volume, |ret|)。"""
    r = _ret(_s(d["close"])).abs()
    v = _s(d["volume"])
    return v.rolling(n, min_periods=n).corr(r)


# ---------------------------------------------------------------------------
# 结构 / 事件
# ---------------------------------------------------------------------------


def k_days_since_high(d: pd.DataFrame, n: int = 60) -> pd.Series:
    """距上次创 N 日新高的天数 / N（0=今天就是新高，1=最久没创新高）。"""
    h = _s(d["high"]).to_numpy()
    m = len(h)
    roll_max = _roll_max(_s(d["high"]), n).to_numpy()
    is_high = np.where(np.isnan(roll_max), False, h >= roll_max - 1e-9)
    idx = np.where(is_high, np.arange(m), -1)
    last = np.maximum.accumulate(idx)
    out = np.full(m, np.nan)
    ok = last >= 0
    out[ok] = (np.arange(m) - last)[ok] / float(n)
    return pd.Series(out)


def k_limit_up_count(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """近 N 日涨停次数（含当日）。一字板/涨停收盘都算。"""
    c = _s(d["close"])
    if "limit_up" in d:
        lu = _s(d["limit_up"])
        flag = (c >= lu - 1e-6) & lu.notna()
    else:  # 无涨跌停价时退化为"涨幅 > 9.8%"
        r = _ret(c)
        flag = r >= 0.098
    return flag.astype("float64").rolling(n, min_periods=n).sum()


def k_gap(d: pd.DataFrame) -> pd.Series:
    """跳空：open_t / close_{t-1} - 1。"""
    o = _s(d["open"])
    c = _s(d["close"])
    pc = _s(d["pre_close"]) if "pre_close" in d else c.shift(1)
    pc = pc.fillna(c.shift(1)).replace(0.0, np.nan)
    return o / pc - 1.0


def k_intraday_ret(d: pd.DataFrame) -> pd.Series:
    """日内收益：close / open - 1（高开低走 vs 低开高走）。"""
    return _s(d["close"]) / _s(d["open"]).replace(0.0, np.nan) - 1.0


def k_turnover_rate(d: pd.DataFrame, n: int = 20) -> pd.Series:
    """换手率：volume / float_share（列可能缺失时为全 NaN）。"""
    if "float_share" not in d:
        return pd.Series(np.full(len(d), np.nan))
    v = _s(d["volume"])
    sh = _s(d["float_share"]).replace(0.0, np.nan)
    return (v / sh).rolling(n, min_periods=max(5, n // 2)).mean()


# ---------------------------------------------------------------------------
# 因子规格表 —— 唯一事实来源
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactorSpec:
    name: str
    fn: Callable[[pd.DataFrame], pd.Series]
    direction: int  # +1 越大越好 / -1 越小越好（先验，可被 IC 实测翻转）
    group: str
    desc: str


def _specs() -> list[FactorSpec]:
    """因子规格表。

    ``direction`` 表示"该指标越大，未来收益越高(+1) / 越低(-1)"。
    它决定 ``FactorScorer`` 在 z-score 时乘的符号，因此**整个系统的选股方向
    由它唯一决定**——改错一个符号，就是在让组合反向押注。

    ⚠️ 本表方向如何确定（P2 实测教训，改之前务必读完）

    本表初版照搬经典因子库（美股教科书口径），实测发现 **27 个因子里有 13 个
    与 A 股数据相反**。分层诊断显示：按错误方向打分后，"策略最想买的前 20%"
    年化仅 10.95%，而"最想避的后 20%"年化 16.52% —— **策略在系统性反向选股**，
    整体跑输等权基准（+12.01%）。

    因此本表方向已按 A 股实测修正。**但"用什么数据定方向"比"改成什么"更关键，
    这里踩过两个坑，都记下来：**

    1. ❌ **不要用"分组平均收益"定方向。** 曾用 `fwd_ret_1` 的五档平均收益做
       判断，得出"几乎所有因子高值端都更好"的结论，与实际 IC 全面矛盾。
       原因是分组平均收益混入了**时序效应**（高波动股在大盘上涨时涨更多，
       这是 beta 不是 alpha）。用它定方向会把 beta 当成选股信号。

    2. ❌ **不要用 RAW（未中性化）数据定方向。** RAW 面板混入市值/行业暴露；
       沪深300 内"低波动"档平均市值 267 亿、"高波动"档 635 亿，两者收益差异
       主要来自风格而非因子本身。

    ✅ **正确口径：中性化后的逐日横截面 RankIC 的符号。**
    逐日横截面秩相关天然剔除了时序效应与市场整体涨跌，中性化又剔除了风格
    暴露，这才是"横截面选股方向"的纯净度量。本表方向与 ICIR_neu 的符号
    一致性为 **27/27**（唯一例外 `gap` 已按其 IC 改为 +1 并标注为薄弱点）。

    关于"先验 vs 拟合"的边界：对有多方经济学共识的因子（如低波动异象、
    反转效应），方向取理论预期；对理论不明确、仅靠数据定方向的（如 `gap`），
    在注释里**显式标注为薄弱点**，并在 P4 成本敏感性中重点复核。**不假装
    每个方向都有坚实的理论支撑。**

    注：逐日 IC 与其均值的同号比例多在 50%~58%，单日 IC 噪声极大，
    因此**绝不能用单日或短窗口 IC 定方向**，必须用全样本均值 + 显著性检验。
    """
    S = FactorSpec
    return [
        # ---- 动量 ----
        # A 股短中期是反转市：散户占比高、追涨杀跌导致过度反应后均值回复，
        # 1 个月~半年尺度上动量因子的横截面 IC 稳定为负（newey-west t 显著）。
        # 故全部取 -1，与美股教科书相反。这一条有充分的理论与实证支持。
        S("mom_20", lambda d: k_mom(d, 20), -1, "动量", "20日动量"),
        S("mom_60", lambda d: k_mom(d, 60), -1, "动量", "60日动量"),
        S("mom_120", lambda d: k_mom(d, 120), -1, "动量", "120日动量"),
        S("mom_60_vol_adj", lambda d: k_mom_risk_adj(d, 60), -1, "动量", "60日风险调整动量"),
        # ---- 反转 ----
        # 注意：这里**不注册 rev_20**。因为 rev_20 ≡ -mom_20，两者横截面相关系数为
        # -1.000，同时放进因子池会让 IC 加权把它们当成两个独立信号（权重反向、
        # 效果叠加），实则是同一个信息被算了两遍。1 月反转效应由 mom_20 承担
        # （mom_20 方向为 -1，等价于一个中期反转因子）。
        S("rev_5", lambda d: k_rev(d, 5), 1, "反转", "5日反转（1周）"),
        # ---- 波动/风险：低波动异象，A 股同样成立（且更显著）----
        S("vol_20", lambda d: k_vol(d, 20), -1, "风险", "20日年化波动"),
        S("vol_60", lambda d: k_vol(d, 60), -1, "风险", "60日年化波动"),
        S("dvol_20", lambda d: k_downside_vol(d, 20), -1, "风险", "20日下行波动"),
        S("mdd_20", lambda d: k_max_drawdown(d, 20), -1, "风险", "20日最大回撤(正=深)"),
        S("skew_20", lambda d: k_skew(d, 20), -1, "风险", "20日收益偏度"),
        S("hl_range_20", lambda d: k_hl_range(d, 20), -1, "风险", "20日平均振幅"),
        # ---- 趋势：乖离率/RSI 越极端越接近超买，取 -1 ----
        S("ma_bias_20", lambda d: k_ma_bias(d, 20), -1, "趋势", "20日乖离率"),
        S("ma_bias_60", lambda d: k_ma_bias(d, 60), -1, "趋势", "60日乖离率"),
        # 金叉在 A 股是**滞后**信号：等均线交叉确认时反转往往已开始，实测 IC 为负
        S("ma_cross_5_20", lambda d: k_ma_cross(d, 5, 20), -1, "趋势", "5/20均线交叉"),
        S("rsi_14", lambda d: k_rsi(d, 14), -1, "趋势", "RSI14"),
        S("stoch_pos_20", lambda d: k_stoch_pos(d, 20), -1, "趋势", "20日随机位置"),
        # ---- 量能 ----
        # 全部取 -1：A 股放量/高换手对应短期情绪高点，后续横截面收益偏低。
        # 注意这一组与美股"量价齐升看多"的直觉相反，但 A 股散户结构与
        # T+1 制度下，高换手更多是博弈拥挤度而非资金流入的度量。
        S("vol_ratio", lambda d: k_volume_ratio(d, 20), -1, "量能", "量比(20日基准)"),
        S("turnover_chg", lambda d: k_turnover_change(d, 5, 20), -1, "量能", "换手突变5/20"),
        # Amihud 非流动性：A 股小盘/低流动性溢价显著，非流动性越高未来收益越高
        S("amihud_20", lambda d: k_amihud(d, 20), 1, "量能", "Amihud非流动性"),
        S("amount_log_20", lambda d: k_amount_log(d, 20), -1, "量能", "20日成交额对数"),
        S("up_down_vol_20", lambda d: k_up_down_vol(d, 20), -1, "量能", "涨跌日量能比"),
        # 量价相关：方向依据偏弱（IC 仅 -0.0013，不显著），保留 -1 但列为弱因子
        S("vol_price_corr_20", lambda d: k_vol_price_corr(d, 20), -1, "量能", "量价相关20"),
        S("turn_rate_20", lambda d: k_turnover_rate(d, 20), -1, "量能", "20日换手率(需股本)"),
        # ---- 结构/事件 ----
        # 距新高天数：越久未创新高 -> 越超跌 -> 反转向上，故 +1
        S("days_since_high_60", lambda d: k_days_since_high(d, 60), 1, "结构", "距60日新高天数"),
        # 涨停次数：短期情绪高点，后续易回落，故 -1
        S("limit_up_cnt_20", lambda d: k_limit_up_count(d, 20), -1, "结构", "20日涨停次数"),
        # 跳空：方向取 +1，依据是中性化后实测 RankIC = +0.021（t 显著）。
        # 注意这与"跳空必回补"的常见直觉相反，本样本也无法给出可靠的经济学
        # 解释（可能来自事件驱动的短期延续）。**这是一个纯数据驱动的方向判断，
        # 属于本因子表的已知薄弱点**，在 P4 的成本敏感性检验中需重点观察：
        # 若扣成本后其贡献消失，应连同方向一起重新审视，而不是保留一个只是
        # "拟合出来"的符号。
        S("gap", lambda d: k_gap(d), 1, "结构", "跳空幅度"),
        # 日内收益：收盘强势 -> 次日反转，故 -1
        S("intraday_ret", lambda d: k_intraday_ret(d), -1, "结构", "日内收益"),
    ]


FACTOR_SPECS: list[FactorSpec] = _specs()

# 名 -> spec 的索引
SPEC_BY_NAME: dict[str, FactorSpec] = {s.name: s for s in FACTOR_SPECS}

# 需要外部数据的因子（股本等），面板里股本缺失时会整体为 NaN，
# IC 检验时会自动跳过有效样本不足的因子。
REQUIRES_SHARES = {"turn_rate_20"}


def spec_names() -> list[str]:
    return [s.name for s in FACTOR_SPECS]


def compute_all(d: pd.DataFrame, names: list[str] | None = None) -> pd.DataFrame:
    """对单只股票的 DataFrame 计算全部（或指定）因子，返回 DataFrame（列为因子名）。"""
    want = names or spec_names()
    out = pd.DataFrame(index=d.index)
    for n in want:
        spec = SPEC_BY_NAME.get(n)
        if spec is None:
            continue
        try:
            out[n] = np.asarray(spec.fn(d), dtype="float64")
        except Exception:  # noqa: BLE001 - 单因子失败不应拖垮整批
            out[n] = np.nan
    return out
