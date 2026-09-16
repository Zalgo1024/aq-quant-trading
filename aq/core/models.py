"""领域模型（与前端 TS 类型 1:1 对齐）。

设计原则：
- 只依赖标准库 + pydantic，便于在回测/模拟/实盘三种模式下复用；
- 金额一律用 float（人民币元），数量为 int（股）；
- 时间统一使用 ISO8601 字符串或 date/datetime，避免时区歧义。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    """订单状态机：PENDING -> SUBMITTED -> (FILLED | PARTIAL | UNFILLED | REJECTED | CANCELLED)"""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLED = "UNFILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class RunMode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Board(str, Enum):
    """板块（决定涨跌停幅度）。"""

    MAIN = "MAIN"        # 主板 ±10%
    STAR = "STAR"        # 科创板 ±20%
    CHINEXT = "CHINEXT"  # 创业板 ±20%
    BSE = "BSE"          # 北交所 ±30%


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


# --------------------------------------------------------------------------
# 行情
# --------------------------------------------------------------------------


class Bar(BaseModel):
    """K 线。

    ⚠️ 关键约定（P1 起）：
    - ``open/high/low/close`` 一律为**后复权价**，用于算收益率与因子
      （后复权保证除权日收益率连续，不会出现假跳空）；
    - 但**撮合与资金结算必须用真实价**——后复权价可能是真实价的十几倍
      （如浦发银行 2026 年后复权 ~160 元、真实价 ~9.2 元），
      若用后复权价计算「100 万本金能买多少手」，会错得离谱。
      因此用 ``adj_factor`` 把后复权价还原成真实价：真实价 = 后复权价 / adj_factor。
    - ``adj_factor`` 默认为 1.0，表示数据本身就是真实价（不复权）。
    """

    symbol: str
    time: datetime
    freq: str = "1d"
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    pre_close: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None
    is_trading: bool = True  # False 表示停牌
    adj_factor: float = 1.0  # 复权因子：后复权价 = 真实价 × adj_factor

    @property
    def open_raw(self) -> float:
        """真实（未复权）开盘价 —— 撮合用。"""
        return self.open / self.adj_factor if self.adj_factor else self.open

    @property
    def close_raw(self) -> float:
        """真实（未复权）收盘价 —— 市值/资金计算用。"""
        return self.close / self.adj_factor if self.adj_factor else self.close

    @property
    def high_raw(self) -> float:
        return self.high / self.adj_factor if self.adj_factor else self.high

    @property
    def low_raw(self) -> float:
        return self.low / self.adj_factor if self.adj_factor else self.low

    @property
    def limit_up_raw(self) -> float | None:
        """真实（未复权）涨停价 —— 与 open_raw 同尺度，撮合判断用。"""
        return self.limit_up / self.adj_factor if (self.limit_up and self.adj_factor) else self.limit_up

    @property
    def limit_down_raw(self) -> float | None:
        """真实（未复权）跌停价。"""
        return self.limit_down / self.adj_factor if (self.limit_down and self.adj_factor) else self.limit_down

    @property
    def pre_close_raw(self) -> float | None:
        return self.pre_close / self.adj_factor if (self.pre_close and self.adj_factor) else self.pre_close

    @property
    def is_limit_up(self) -> bool:
        return self.limit_up is not None and self.close >= self.limit_up - 1e-6

    @property
    def is_limit_down(self) -> bool:
        return self.limit_down is not None and self.close <= self.limit_down + 1e-6


# --------------------------------------------------------------------------
# 账户 / 持仓
# --------------------------------------------------------------------------


class Position(BaseModel):
    """持仓。`available` 为 T+1 解冻后的可卖数量。"""

    symbol: str
    qty: int = 0                 # 总持仓
    available: int = 0           # 可卖（T+1）
    avg_cost: float = 0.0
    last_price: float = 0.0
    realized_pnl: float = 0.0

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_cost) * self.qty


class Account(BaseModel):
    account_id: str = "sim"
    cash: float = 1_000_000.0
    frozen: float = 0.0
    positions: dict[str, Position] = Field(default_factory=dict)
    realized_pnl: float = 0.0
    updated_at: datetime | None = None

    @property
    def market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_asset(self) -> float:
        return self.cash + self.frozen + self.market_value

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())


# --------------------------------------------------------------------------
# 订单 / 成交
# --------------------------------------------------------------------------


class Order(BaseModel):
    oid: str
    symbol: str
    side: Side
    qty: int
    type: OrderType = OrderType.LIMIT
    limit_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    strategy_id: str = "default"
    account_id: str = "sim"
    created_at: datetime | None = None
    reason: str = ""  # 拒单/未成交原因

    @field_validator("qty")
    @classmethod
    def _qty_positive_and_lot(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("qty 必须为正")
        if v % 100 != 0:
            raise ValueError("A 股买入须为 100 股（1 手）的整数倍")
        return v


class Fill(BaseModel):
    oid: str
    symbol: str
    side: Side
    filled_qty: int = 0
    avg_price: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    slippage_cost: float = 0.0
    status: OrderStatus = OrderStatus.FILLED
    reason: str = ""
    ts: datetime | None = None

    @property
    def total_cost(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee


# --------------------------------------------------------------------------
# 信号 / 预测
# --------------------------------------------------------------------------


class FactorContrib(BaseModel):
    """单个因子对最终评分的贡献（可解释性）。

    注意：``contrib`` 使用全精度值（不预先 round），
    以保证 ``Σ contrib == Prediction.score``，前端瀑布图才能精确对账。
    """

    name: str
    weight: float
    value: float          # 归一化后的因子得分 [0,1]
    contrib: float = 0.0  # weight * value，用于瀑布图

    def model_post_init(self, __context: Any) -> None:  # noqa: D105
        # 仅在未显式传入 contrib 时按 weight*value 补齐
        if self.contrib == 0.0 and self.weight and self.value:
            self.contrib = self.weight * self.value


class Prediction(BaseModel):
    """模型输出的个股预测（对齐 signal.json）。"""

    symbol: str
    time: datetime | None = None
    score: float = 0.0                            # 综合评分，越高越看多
    confidence: float = 0.0                       # 置信度 [0,1]
    direction: Direction = Direction.HOLD
    factor_contrib: list[FactorContrib] = Field(default_factory=list)
    model_version: str = "unknown"
    explain: str = ""                             # 人类可读的一句话解释


class Signal(BaseModel):
    id: str = ""
    symbol: str
    side: Side
    strength: float = 0.0      # [0,1]
    confidence: float = 0.0    # [0,1]
    trigger_factor: str = ""
    ts: datetime | None = None
    source: str = "model"


# --------------------------------------------------------------------------
# 异常检测
# --------------------------------------------------------------------------


class Anomaly(BaseModel):
    time: datetime | None = None
    symbol: str = ""
    type: str = ""            # 量价异动 / 疑似操纵 / 风格切换 ...
    z_score: float = 0.0
    severity: Literal["info", "warn", "severe"] = "info"
    detail: str = ""


# --------------------------------------------------------------------------
# 回测结果
# --------------------------------------------------------------------------


class EquityPoint(BaseModel):
    time: datetime
    equity: float
    benchmark: float | None = None


class BacktestMetrics(BaseModel):
    total_return: float = 0.0
    annual_return: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0
    calmar: float = 0.0
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    turnover: float = 0.0
    # 以下三项**依赖基准序列**；未接入基准时为 None（不伪造）
    information_ratio: float | None = None
    alpha: float | None = None
    beta: float | None = None
    # 防过拟合
    cscv: float | None = None
    deflated_sharpe: float | None = None


class BacktestResult(BaseModel):
    run_id: str
    start: date | None = None
    end: date | None = None
    equity: list[EquityPoint] = Field(default_factory=list)
    drawdown: list[EquityPoint] = Field(default_factory=list)
    metrics: BacktestMetrics = Field(default_factory=BacktestMetrics)
    trades: list[Fill] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    # 回测过程诊断（非绩效指标）：平均持仓数、平均仓位、满仓天数等。
    # 单独放而不是塞进 metrics，是因为这些不是"策略好不好"的度量，
    # 而是"回测有没有按预期执行"的度量 —— 低仓位会同时压低 beta 和收益，
    # 不盯住它会把工程 bug 误读成策略特性。
    diagnostics: dict[str, Any] = Field(default_factory=dict)
