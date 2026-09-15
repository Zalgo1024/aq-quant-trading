"""执行层：行情源与交易网关的抽象，以及模拟/实盘实现。"""

from aq.execution.feed import MarketFeed, MockFeed, HistoricalFeed, ReplayFeed  # noqa: F401
from aq.execution.gateway import TradingGateway, GatewayError, create_gateway  # noqa: F401
from aq.execution.sim_gateway import SimGateway  # noqa: F401

__all__ = [
    "MarketFeed",
    "MockFeed",
    "HistoricalFeed",
    "ReplayFeed",
    "TradingGateway",
    "GatewayError",
    "SimGateway",
    "create_gateway",
]
