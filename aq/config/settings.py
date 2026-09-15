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
        池子规模上限，防止全市场 5000+ 只把内存吃光
    """

    index: str = "hs300"
    exclude: list[str] = Field(default_factory=lambda: ["ST", "DELISTED"])
    min_list_days: int = 60
    min_turnover: float = 0.0
    lookback: int = 20
    max_symbols: int = 500


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
    initial_cash: float = 1_000_000.0
    benchmark: str = "000300"
    cost: CostConfig = Field(default_factory=CostConfig)


class RiskConfig(BaseModel):
    single_stock_max: float = 0.10
    industry_max: float = 0.30
    total_position_max: float = 0.95
    stop_loss: float = 0.08
    max_drawdown: float = 0.20
    liquidity_min_turnover: float = 1e8


class ModelConfig(BaseModel):
    cross_section: str = "lightgbm"
    time_series: str = "lstm"
    sentiment: str = "llm_api"
    top_k: int = 20
    score_threshold: float = 0.55


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
