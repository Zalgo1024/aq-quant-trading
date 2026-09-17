"""配置系统：yaml 文件 + 环境变量覆盖。

用法::

    from aq.config import load_settings
    s = load_settings("config/paper.yaml")     # 指定文件
    s = load_settings()                        # 默认 config/base.yaml
    s = load_settings(mode="paper")            # base.yaml + paper.yaml 叠加

约定优先级（低 -> 高）：
    base.yaml  <  <mode>.yaml  <  环境变量 (AQ_*)  <  显式 kwargs
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from aq.core.models import RunMode

# 项目根目录：.../量化交易/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class DataConfig(BaseModel):
    source: str = "mock"          # mock | akshare | tushare
    adjust: str = "post"          # post(后复权) | pre(前复权) | none
    start: str = "2018-01-01"
    end: str = "2026-06-30"
    cache_dir: str = "data_cache"


class UniverseConfig(BaseModel):
    """股票池筛选（由 ``aq.data.universe.UniverseSelector`` 消费）。

    index:
        hs300 / zz500 / sz50 / zz1000 / all
    exclude:
        预留，当前固定排除 ST、*ST、退市
    min_list_days:
        次新股过滤：上市不足 N 个自然日不参与（需 stock_list.list_date）
    min_turnover:
        最近 lookback 日日均成交额下限（元），0 = 不过滤
    max_symbols:
        池子规模上限，防止全市场 5000+ 只把内存吃光。0 = 不截断。
        **默认 0（不截断）**：``max_symbols > 0`` 时按 ``stock_list.parquet``
        的代码序取前 N 只，会无限期排除代码靠后的股票（北交所 8xxxxx/4xxxxx、
        以及沪市 6xxxxx 里排在后面的部分），使池子**永不收敛到全市场**。
        研究场景要的是完整池子，截断属于残余偏差，故默认关闭。
    """

    index: str = "hs300"
    exclude: list[str] = Field(default_factory=lambda: ["ST", "DELISTED"])
    min_list_days: int = 60
    min_turnover: float = 0.0
    lookback: int = 20
    max_symbols: int = 0


class CostConfig(BaseModel):
    commission: float = 0.00025
    min_commission: float = 5.0
    stamp_tax: float = 0.0005
    transfer_fee: float = 0.00001
    slippage: float = 0.001


class BacktestConfig(BaseModel):
    start: str = "2018-01-01"
    end: str = "2026-06-30"
    freq: str = "1d"
    #: 调仓间隔（交易日）。显式配置优先级高于 ``freq``；不设时从 ``freq``
    #: 解析（``1d``→1、``10d``→10）。日频调仓的成本会吃掉全部 alpha。
    rebalance_days: int | None = None
    initial_cash: float = 1_000_000.0
    benchmark: str = "000300"
    # 强制重建因子面板缓存。默认 False —— 面板按 (universe, start, end, factors)
    # 哈希缓存，换池子或换区间会自动换文件名。**只有在底层数据变了**
    # （典型如补了股本 → mktcap 变 → 中性化结果变）才需要置 True，
    # 否则会一直吃到旧缓存，看起来"改了数据没生效"。
    panel_force: bool = False
    cost: CostConfig = Field(default_factory=CostConfig)


class RiskConfig(BaseModel):
    single_stock_max: float = 0.10
    industry_max: float = 0.30
    total_position_max: float = 0.95
    stop_loss: float = 0.08
    max_drawdown: float = 0.20
    # 单日成交额门槛（元）。**默认关闭（0）**，理由见下。
    #
    # 这里曾经是 1e8（1 亿），而股票池按"20 日日均 ≥ 2e7"选股 ——
    # 门槛差 5 倍、且一个是 20 日均值一个是单日值。后果：20 只目标里
    # 平均只有 5.4 只买得进，平均仓位被死死压在 26.7%，
    # 2059 笔买单以"流动性不足"被拒（占全部拒单的 99.8%），
    # 而日志里一句提示都没有。
    #
    # 为什么默认不是"对齐到 2e7"：那样仍然是用**单日**值去重复
    # **20 日均值**的过滤，会在个股成交额临时回落的日子上造成
    # "部分成交"—— 组合被随机削掉一部分，而不是按信号削。
    # 正确分工是：
    #   · 能不能投  → 交给 universe.min_turnover（20 日均值，选股时过滤）
    #   · 能吃多少  → 交给 liquidity_order_ratio（与组合规模挂钩）
    # 两道门槛各司其职，不再用两个不同口径互相打架。
    # 需要额外保守时可自行设成正数（注意必须与 universe.min_turnover 同量级）。
    liquidity_min_turnover: float = 0.0
    # 相对流动性门槛：单笔订单金额不得超过当日成交额的 1/ratio。
    # 绝对门槛无法随组合规模缩放（1 亿对 100 万的组合过严、对 10 亿过松），
    # 这条相对约束才是真正与"冲击成本"挂钩的那一道。
    # 0 或负 = 不启用。
    liquidity_order_ratio: float = 10.0


class ModelConfig(BaseModel):
    cross_section: str = "lightgbm"
    time_series: str = "lstm"
    sentiment: str = "llm_api"
    top_k: int = 20
    score_threshold: float = 0.55

    # ---- P2：因子打分 ----
    # 权重来源："prior"（内置先验）| "ic"（读因子研究实测 IC 自动定权）
    weight_source: str = "prior"
    # ---- 打分口径中性化（2026-09-17 新增）----
    # 修的是一个**口径错位**：IC 是在「行业+市值中性化」口径上估的
    # （factor_research.py --neutralize），但回测打分路径原先**不做中性化**，
    # 只做全市场横截面 z-score。后果是组合实际在做"raw 口径排序"——
    # 低 PB 在全市场口径下 ≈ 买建筑 + 钢铁（深度价值行业押注），
    # 而不是行业内的横截面选股。IC 与组合口径不一致，回测数字就不可信。
    #
    # True（推荐）：打分前对全部因子做行业 + 市值中性化，与 IC 估计同口径。
    # False：保留旧的 raw 口径，仅用于做 A/B 对照复盘，不要用来下结论。
    #
    # 注：中性化需要面板带 industry / mktcap 两列；缺任一列会自动退回 raw 并告警。
    score_neutralize: bool = True
    # weight_source="ic" 时读取的 summary.csv（相对项目根或绝对路径）。
    # 留空则自动取 runtime/factor_research/ 下最新的一次结果。
    ic_summary_path: str = ""
    # IC 定权方式：icir（强度/稳定性兼顾，推荐）| ic（只看强度）
    ic_weight_mode: str = "icir"
    # 纳入门槛：|RankIC| >= min_abs_ic 且 p <= max_p 的因子才有权重
    ic_min_abs: float = 0.01
    ic_max_p: float = 0.10
    # 单因子权重上限（防止一个因子垄断组合）
    ic_cap: float = 0.25
    # 幂次压缩指数：w ∝ |v| ** ic_shrink（1.0 = 线性不压缩）。
    # 取值 >1 会把强因子进一步拉开差距。
    ic_shrink: float = 1.0
    # ---- 成本感知定权（B1，2026-09-17 新增）----
    # 定权值改成 ``|v| / turnover ** ic_cost_penalty``，turnover 取自
    # ``summary.csv`` 的 ``turnover`` 列（因子自身的截面排序换手，已有、无需新数据）。
    #
    # 解决的问题：原口径按 |RankICIR| 定权、**完全不看换手**，于是权重恰好压在
    # 最贵的因子上 —— full_neu_v2 实测组合加权平均换手 0.2641，权重前三的
    # vol_ratio / gap / intraday_ret 换手分别 0.51 / 0.79 / 0.83，而换手仅
    # 0.015~0.021 的估值三因子（bp/ep/sp）合计只拿到 4.5% 权重。
    # 这与"交易成本吃掉 alpha"这个瓶颈正面冲突。
    #
    # 取值：0.0（默认）= 现状，逐字不变（保住已有缓存与历史结论）；
    #       0.5 = 半分惩罚；1.0 = 完全按净口径 |RankICIR| / turnover。
    # 反事实推算（penalty=1）：加权换手 0.2641 -> 0.0731（−72%），
    # 估值三因子权重 4.54% -> 36.9%。
    #
    # ⚠️ turnover 是**样本内**估计量，用它定权本身是一个新自由度，
    # 有可能把一种过拟合换成另一种。**验收必须走 `ic_wf`（滚动重估）**，
    # 且只认 CSCV 之后的 PBO / DSR / 超额 t，不认单次回测净值。
    ic_cost_penalty: float = 0.0
    # 是否启用因子筛选：显著性门控 + 中性化抗性检查 + 相关性去冗余。
    # 关掉会让高相关因子重复计权（P2 首轮 A/B 对照已证实会显著变差）。
    ic_select: bool = True
    # 中性化抗性门控：|icir_neu| / |icir_raw| 低于此值即剔除（默认 0.5）。
    #
    # 为什么必须显式配这一项：`scripts/ab_weight_test.py` 一直有 `--icir-ratio`
    # 这个开关，但**从未被透传到打分器**，落在这里的永远是函数默认值 ——
    # 典型"配置看起来能调、其实调不动"。更早的实现连分母都用错了
    # （用 rank_icir 而非 icir_raw，在 full_neu 变体下比值恒为 1），
    # 两层叠加使这道门彻底失效。见 aq/factors/ic.py:select_factors 的注释。
    ic_min_icir_ratio: float = 0.5
    # 相关性去冗余阈值
    ic_corr_threshold: float = 0.85
    # IC 权重加载失败时是否直接报错（默认 True = 报错）。
    #
    # 为什么默认严格：曾经出现过 summary.csv 列名不符合契约（rank_icir 被
    # 误改名），加载失败被静默吞掉退回先验权重，导致 A/B 对照实验的
    # "IC 加权组"实际跑的还是先验权重，却得出"IC 加权无效"的假结论。
    # 研究场景下，**响亮的报错远比安静的降级有价值**。
    # 线上实盘若不想因研究产物缺失而中断，可显式设为 False。
    ic_strict: bool = True

    # 因子子集（研究用 ablation）：非空时**只有**列出的因子能拿到权重。
    #
    # 为什么需要它：IC 加权按 |RankICIR| 定权、完全不管换手，于是低换手的
    # 估值因子（bp/ep/sp）在全因子组合里只分到约 5.7% 权重 —— 想单独检验
    # "估值因子到底有没有 alpha"，就必须能把其余因子按下去。
    #
    # 子集模式与全因子模式的**口径差异**（务必知情，否则会误读结果）：
    #   1. 跳过组内 min-max 归一化 —— N=3 时最小值恒被压成 0（伪影）；
    #   2. 跳过单因子 cap —— cap=0.25 在 3 因子里会强制等权、丢失 ICIR 区分度。
    # 权重改为直接按 |RankICIR| 比例分配。若子集内权重全零，**直接抛错**
    # 而非退化为全因子等权（后者会产出一份贴错标签的回测）。
    #
    # 空列表（默认）= 不限制，行为与历史版本逐字一致。
    factor_include: list[str] = Field(default_factory=list)

    # ---- Walk-forward 定权（weight_source="ic_wf"）----
    # 只用截至时点的历史 IC 重新筛因子、重新定权，使日收益真正样本外。
    # 见 aq/factors/walkforward.py 与报告 7.14。
    wf_window: int = 504          # 回看窗口（交易日，约 2 年）
    wf_refit_every: int = 60      # 重新定权间隔（交易日，约一季度）
    wf_corr_mode: str = "ic"      # 去冗余矩阵口径：ic（滚动，无前视）/ static（全样本）
    # 未中性化变体的 summary 路径。**中性化抗性门控的分母**必须用它，
    # 否则在 full_neu 变体下 |icir_neu|/|rank_icir| 恒为 1，门控形同虚设。
    ic_raw_summary_path: str = ""


class FrontendConfig(BaseModel):
    theme: str = "dark"
    up_color: str = "red"
    currency: str = "CNY"


class ExecutionConfig(BaseModel):
    broker: str = "sim"           # sim | qmt | ptrade | jq
    account_id: str = "sim"
    persist_path: str = "runtime/sim_account.json"
    poll_interval: float = 3.0


class Settings(BaseModel):
    mode: RunMode = RunMode.PAPER
    data: DataConfig = Field(default_factory=DataConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    frontend: FrontendConfig = Field(default_factory=FrontendConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)

    # 额外透传字段（前端/实验用）
    extra: dict[str, Any] = Field(default_factory=dict)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env(data: dict) -> dict:
    """支持 AQ_MODE / AQ_DATA__SOURCE 形式的环境变量覆盖。"""
    out = dict(data)
    for key, val in os.environ.items():
        if not key.startswith("AQ_"):
            continue
        path = key[3:].lower().split("__")
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        # 尽力做类型转换
        parsed: Any = val
        for caster in (int, float):
            try:
                parsed = caster(val)  # type: ignore[assignment]
                break
            except (TypeError, ValueError):
                continue
        else:
            if val.lower() in ("true", "false"):
                parsed = val.lower() == "true"
        node[path[-1]] = parsed
    return out


def load_settings(
    path: str | Path | None = None,
    mode: str | RunMode | None = None,
    **overrides: Any,
) -> Settings:
    """加载配置。

    Parameters
    ----------
    path:
        显式指定配置文件；给出时忽略 ``mode`` 叠加。
    mode:
        运行模式名（backtest/paper/live），会叠加 ``config/<mode>.yaml``。
    **overrides:
        显式字段覆盖，如 ``load_settings(mode="paper", risk={"single_stock_max": 0.05})``。
    """
    data: dict[str, Any] = _read_yaml(CONFIG_DIR / "base.yaml")

    if path is not None:
        data = _deep_merge(data, _read_yaml(Path(path)))
    else:
        mode_name = mode.value if isinstance(mode, RunMode) else (mode or data.get("mode", "paper"))
        data = _deep_merge(data, _read_yaml(CONFIG_DIR / f"{mode_name}.yaml"))
        data.setdefault("mode", mode_name)

    data = _apply_env(data)

    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k] = _deep_merge(data[k], v)
        else:
            data[k] = v

    return Settings(**data)


_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    """进程级单例（简单缓存）。"""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


def set_settings(s: Settings) -> None:
    global _SETTINGS
    _SETTINGS = s
