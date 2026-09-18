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

#: 资产类别 → 费率**覆盖**。未列出的键一律走上面的模块常量。
#:
#: 为什么必须分类：**ETF 免印花税、免过户费**（现实中如此）。原实现对所有
#: 标的卖出都收 0.05% 印花税（``rules.py`` 旧版第 102 行），用在 ETF 上
#: 会让「实际成本与预估偏差 < 20%」这条验收判据**直接失败**。
#:
#: ⚠️ ``stock`` 的覆盖是**空的** —— 它走模块常量，保证建库以来所有个股
#: 回测数字与缓存**逐字节不变**。改这个字典时先问一句：动了 stock 吗？
FEE_OVERRIDES: dict[str, dict[str, float]] = {
    "stock": {},
    "etf": {
        "stamp_tax": 0.0,
        "transfer_fee": 0.0,
        # 最低佣金单独可覆盖：用户要去谈「ETF 免最低 5 元」。一个 6250 元的
        # 单子按万 0.5 本应 0.31 元，被 5 元地板抬成 16 倍 —— 谈成后把这里
        # 改成 0.0 即可，是**确定性可得**的收益（见 docs/小额实盘方案与判据.md §2.1）。
        "min_commission": COMMISSION_MIN,
    },
}

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


def fee_params(asset_class: str = "stock") -> dict[str, float]:
    """返回某资产类别的完整费率表（模块常量 + ``FEE_OVERRIDES`` 覆盖）。

    未登记的类别**直接报错**（不静默退化成 stock）—— 一个贴错标签的
    费率会让回测数字悄悄偏掉，而项目已经为「配置看起来能调、其实调不动」
    栽过多次（见 ``settings.py`` 的 ``ic_min_icir_ratio`` 注释）。
    """
    base = {
        "commission": COMMISSION_RATE,
        "min_commission": COMMISSION_MIN,
        "stamp_tax": STAMP_TAX_RATE,
        "transfer_fee": TRANSFER_FEE_RATE,
    }
    if asset_class not in FEE_OVERRIDES:
        raise KeyError(
            f"未知资产类别 {asset_class!r}。已知：{', '.join(sorted(FEE_OVERRIDES))}"
        )
    base.update(FEE_OVERRIDES[asset_class])
    return base


def calc_fees(
    side_is_sell: bool,
    price: float,
    qty: int,
    asset_class: str = "stock",
) -> dict[str, float]:
    """计算单笔交易费用。

    ``asset_class="stock"``（默认）**逐字复刻**历史实现：佣金
    ``max(额×万2.5, 5元)``、印花税仅卖出 0.05%、过户费万 0.1。
    ``asset_class="etf"`` 则免印花税与过户费。

    ⚠️ **不要用「代码段」自动推断资产类别**。实测反例：``510080`` /
    ``510081``（2004 年成立）、``560002``（2006）、``560003``（2007）代码
    都落在 ETF 段，但它们是与 ETF 同代码段的**普通开放式基金**
    （见 ``scripts/probes/probe_etf_reach.py`` 第 2 段）。
    类别必须由调用方显式给出（ETF 池成员关系来自 ``etf_list.parquet``）。
    """
    p = fee_params(asset_class)
    turnover = price * qty
    commission = max(turnover * p["commission"], p["min_commission"])
    stamp = turnover * p["stamp_tax"] if side_is_sell else 0.0
    transfer = turnover * p["transfer_fee"]
    return {
        "commission": round(commission, 2),
        "stamp_tax": round(stamp, 2),
        "transfer_fee": round(transfer, 2),
    }
