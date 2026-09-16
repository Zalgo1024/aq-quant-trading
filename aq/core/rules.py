"""A 股交易规则常量（费率、涨跌停幅度、交易单位等）。

所有数值集中此处，回测/模拟/实盘共用，避免"三套规则"导致结果不一致。
"""

from __future__ import annotations

from aq.core.models import Board

# --------------------------------------------------------------------------
# 交易费用
# --------------------------------------------------------------------------

# 佣金：万分之 2.5，单笔最低 5 元（买卖双向）
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0

# 印花税：千分之 0.5，**仅卖出**收取
STAMP_TAX_RATE = 0.0005

# 过户费：万分之 0.1（沪深两市现均为双向）
TRANSFER_FEE_RATE = 0.00001

# 默认滑点（千分之 1）
DEFAULT_SLIPPAGE = 0.001

# --------------------------------------------------------------------------
# 交易规则
# --------------------------------------------------------------------------

LOT_SIZE = 100          # 1 手 = 100 股
T_PLUS = 1              # T+1：当日买入次日方可卖出
ALLOW_SHORT = False     # 不支持裸卖空

# 涨跌停幅度（按板块）
LIMIT_PCT: dict[Board, float] = {
    Board.MAIN: 0.10,
    Board.STAR: 0.20,
    Board.CHINEXT: 0.20,
    Board.BSE: 0.30,
}

# ST 股票主板 ±5%
ST_LIMIT_PCT = 0.05

# --------------------------------------------------------------------------
# 风控默认阈值
# --------------------------------------------------------------------------

DEFAULT_RISK = {
    "single_stock_max": 0.10,      # 单票市值占比上限
    "industry_max": 0.30,          # 单行业占比上限
    "total_position_max": 0.95,    # 总仓位上限
    "stop_loss": 0.08,             # 个股止损线
    "max_drawdown": 0.20,          # 组合最大回撤警戒
    # 单日成交额门槛。默认 0 = 不启用（流动性改由 universe.min_turnover
    # 与 liquidity_order_ratio 分工负责；旧值 1e8 会把仓位压到 27%）
    "liquidity_min_turnover": 0.0,
    # 相对流动性：单笔订单金额 ≤ 当日成交额 / ratio。0 = 不启用
    "liquidity_order_ratio": 10.0,
}

# --------------------------------------------------------------------------
# 其它
# --------------------------------------------------------------------------

# 一年交易日（年化换算用）
TRADING_DAYS_PER_YEAR = 242

# 买入后不可卖出的天数（T+1）
def board_of(symbol: str) -> Board:
    """根据代码推断板块。

    - 688xxx        -> 科创板
    - 300xxx/301xxx -> 创业板
    - 8xxxxx/4xxxxx -> 北交所
    - 其余          -> 主板
    """
    code = symbol.split(".")[0]
    if code.startswith("688"):
        return Board.STAR
    if code.startswith(("300", "301")):
        return Board.CHINEXT
    if code.startswith(("8", "4")) and len(code) == 6:
        return Board.BSE
    return Board.MAIN


def limit_prices(pre_close: float, symbol: str, is_st: bool = False) -> tuple[float, float]:
    """返回 (涨停价, 跌停价)，按板块与 ST 状态计算，四舍五入到分。"""
    board = board_of(symbol)
    pct = ST_LIMIT_PCT if is_st else LIMIT_PCT[board]
    up = round(pre_close * (1 + pct), 2)
    down = round(pre_close * (1 - pct), 2)
    return up, down


def calc_fees(side_is_sell: bool, price: float, qty: int) -> dict[str, float]:
    """计算单笔交易费用。"""
    turnover = price * qty
    commission = max(turnover * COMMISSION_RATE, COMMISSION_MIN)
    stamp = turnover * STAMP_TAX_RATE if side_is_sell else 0.0
    transfer = turnover * TRANSFER_FEE_RATE
    return {
        "commission": round(commission, 2),
        "stamp_tax": round(stamp, 2),
        "transfer_fee": round(transfer, 2),
    }
